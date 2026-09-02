from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import frappe
from frappe import _
from frappe.utils import (
    add_months,
    cint,
    flt,
    get_first_day,
    get_last_day,
    getdate,
    nowdate,
)

from time_tracker.events.employee import ensure_time_tracker_for_employee
from time_tracker.settings import get_company_setting, get_enabled_settings
from time_tracker.payroll import (
    AUTOMATION_MODE_FIXED_SALARY,
    AUTOMATION_MODE_TIME_TRACKER,
    PAYROLL_ENTRY_AUTOMATION_MODE_FIELD,
    reconcile_time_tracker_salary_slips_for_payroll_entry,
)


TIME_TRACKER_DOCTYPE = "Time Tracker"


def handle_new_employee(doc, method: str | None = None) -> None:
    """Run configured onboarding actions without blocking Employee creation.

    Each step has its own database savepoint. A configuration or HRMS validation
    problem is written to Error Log while the Employee itself remains created.
    """

    del method

    rule = get_company_setting(doc.company, enabled_only=True)
    if not rule or not cint(rule.enable_employee_automation):
        return

    if cint(rule.auto_assign_salary_structure):
        _run_employee_step(
            doc.name,
            _("Salary Structure Assignment"),
            lambda: ensure_salary_structure_assignment(doc, rule),
        )

    # Assignment submission normally performs this sync through its hook. Run
    # it explicitly as well so a pre-existing assignment is handled and a
    # future joining date can provision the tracker immediately.
    if cint(rule.auto_create_time_tracker):
        reference_date = doc.get("date_of_joining") or nowdate()
        _run_employee_step(
            doc.name,
            _("Time Tracker"),
            lambda: sync_employee_time_tracker_mode(
                doc.name,
                reference_date=reference_date,
                allow_create=True,
            ),
        )

    if cint(rule.auto_create_appraisal):
        _run_employee_step(
            doc.name,
            _("Appraisal"),
            lambda: ensure_employee_appraisal(doc, rule),
        )

    if cint(rule.auto_allocate_leave):
        _run_employee_step(
            doc.name,
            _("Leave Policy Assignment"),
            lambda: ensure_leave_policy_assignment(doc, rule),
        )


def handle_salary_structure_assignment_change(
    doc,
    method: str | None = None,
) -> None:
    """Synchronise tracker availability from the effective Salary Structure."""

    del method

    employee = doc.get("employee")
    if not employee:
        return

    company = doc.get("company") or frappe.db.get_value(
        "Employee", employee, "company"
    )
    rule = get_company_setting(company, enabled_only=True)
    if (
        not rule
        or not cint(rule.enable_employee_automation)
        or not cint(rule.auto_create_time_tracker)
    ):
        return

    _run_employee_step(
        employee,
        _("Time Tracker Salary Structure Sync"),
        lambda: sync_employee_time_tracker_mode(
            employee,
            reference_date=nowdate(),
            allow_create=True,
        ),
    )


def ensure_salary_structure_assignment(employee, rule) -> str | None:
    """Create and submit the configured assignment for a new Employee.

    Employee Grade defaults take priority. The company-level structure/base are
    fallbacks. Payroll type is never stored on Employee: it is read directly
    from ``Salary Structure.custom_based_on_time_tracker``.
    """

    employee = _employee_doc(employee)
    if not employee or not employee.name:
        return None

    from_date = employee.get("date_of_joining") or nowdate()
    existing = frappe.get_all(
        "Salary Structure Assignment",
        filters={
            "employee": employee.name,
            "from_date": from_date,
            "docstatus": ["!=", 2],
        },
        fields=["name", "docstatus"],
        order_by="docstatus desc, creation desc",
        limit_page_length=1,
    )
    if existing:
        assignment = frappe.get_doc(
            "Salary Structure Assignment", existing[0].name
        )
        if cint(assignment.docstatus) == 0:
            assignment.flags.ignore_permissions = True
            assignment.submit()
        return assignment.name

    grade_defaults = frappe._dict()
    if employee.get("grade"):
        grade_defaults = frappe.db.get_value(
            "Employee Grade",
            employee.grade,
            ["default_salary_structure", "default_base_pay"],
            as_dict=True,
        ) or frappe._dict()

    salary_structure = (
        grade_defaults.get("default_salary_structure")
        or rule.get("default_salary_structure")
    )
    if not salary_structure:
        return None

    structure = frappe.db.get_value(
        "Salary Structure",
        salary_structure,
        ["company", "currency", "docstatus", "is_active"],
        as_dict=True,
    )
    if not structure:
        frappe.throw(
            _("Salary Structure {0} does not exist.").format(
                frappe.bold(salary_structure)
            )
        )
    if (
        structure.company != employee.company
        or cint(structure.docstatus) != 1
        or structure.is_active != "Yes"
    ):
        frappe.throw(
            _(
                "Salary Structure {0} must be submitted, active, and belong "
                "to Employee company {1}."
            ).format(
                frappe.bold(salary_structure),
                frappe.bold(employee.company),
            )
        )

    base = grade_defaults.get("default_base_pay")
    if base is None or flt(base) == 0:
        base = rule.get("default_assignment_base") or 0

    assignment = frappe.get_doc(
        {
            "doctype": "Salary Structure Assignment",
            "employee": employee.name,
            "salary_structure": salary_structure,
            "from_date": from_date,
            "company": employee.company,
            "currency": structure.currency,
            "base": flt(base),
            "variable": flt(rule.get("default_assignment_variable")),
            "income_tax_slab": rule.get("income_tax_slab"),
            "payroll_payable_account": rule.get("payroll_payable_account"),
        }
    )
    assignment.flags.ignore_permissions = True
    assignment.insert(ignore_permissions=True)
    assignment.submit()
    return assignment.name


def get_effective_salary_structure_assignment(
    employee: str,
    on_date=None,
) -> frappe._dict | None:
    """Return the latest submitted assignment effective on ``on_date``."""

    if not employee:
        return None

    on_date = getdate(on_date or nowdate())
    rows = frappe.get_all(
        "Salary Structure Assignment",
        filters={
            "employee": employee,
            "docstatus": 1,
            "from_date": ["<=", on_date],
        },
        fields=[
            "name",
            "salary_structure",
            "from_date",
            "company",
            "currency",
            "payroll_payable_account",
        ],
        order_by="from_date desc, modified desc",
        limit_page_length=1,
    )
    if not rows:
        return None

    assignment = frappe._dict(rows[0])
    structure = frappe.db.get_value(
        "Salary Structure",
        assignment.salary_structure,
        [
            "docstatus",
            "is_active",
            "salary_slip_based_on_timesheet",
            "custom_based_on_time_tracker",
            "company",
            "currency",
        ],
        as_dict=True,
    )
    if not structure:
        return None

    assignment.update(structure)
    return assignment


def sync_employee_time_tracker_mode(
    employee: str,
    *,
    reference_date=None,
    allow_create: bool = True,
) -> frappe._dict:
    """Apply the effective Salary Structure's hourly/fixed tracker behavior."""

    result = frappe._dict(
        employee=employee,
        salary_structure=None,
        is_hourly=False,
        tracker=None,
        created=False,
        widget_disabled=False,
    )

    assignment = get_effective_salary_structure_assignment(
        employee,
        reference_date,
    )
    if not assignment:
        return result

    result.salary_structure = assignment.salary_structure
    is_hourly = bool(
        cint(assignment.docstatus) == 1
        and assignment.is_active == "Yes"
        and cint(assignment.custom_based_on_time_tracker) == 1
    )
    result.is_hourly = is_hourly

    tracker = frappe.db.get_value(
        TIME_TRACKER_DOCTYPE,
        {"employee": employee},
        ["name", "enable_browser_widget"],
        as_dict=True,
    )

    if is_hourly and allow_create:
        if not tracker:
            tracker_name = ensure_time_tracker_for_employee(employee)
            result.tracker = tracker_name
            result.created = bool(tracker_name)
        else:
            result.tracker = tracker.name
        return result

    if tracker:
        # A later fixed-salary assignment must not delete historical tracker/log
        # data or overwrite the employee's own widget preference. The employee
        # is simply excluded from Time Tracker payroll by the structure flag.
        result.tracker = tracker.name

    return result


def ensure_employee_appraisal(employee, rule) -> str | None:
    """Create one draft Appraisal for the configured cycle."""

    employee = _employee_doc(employee)
    if not employee or employee.get("status") != "Active":
        return None

    appraisal_cycle = rule.get("appraisal_cycle")
    if not appraisal_cycle:
        return None

    existing = frappe.db.get_value(
        "Appraisal",
        {
            "employee": employee.name,
            "appraisal_cycle": appraisal_cycle,
            "docstatus": ["!=", 2],
        },
        "name",
    )
    if existing:
        return existing

    appraisal_template = frappe.db.get_value(
        "Appraisee",
        {"parent": appraisal_cycle, "employee": employee.name},
        "appraisal_template",
    )
    if not appraisal_template and employee.get("designation"):
        appraisal_template = frappe.db.get_value(
            "Designation",
            employee.designation,
            "appraisal_template",
        )
    appraisal_template = appraisal_template or rule.get("appraisal_template")
    if not appraisal_template:
        return None

    cycle = frappe.db.get_value(
        "Appraisal Cycle",
        appraisal_cycle,
        ["company", "status", "kra_evaluation_method"],
        as_dict=True,
    )
    if not cycle or cycle.company != employee.company or cycle.status == "Completed":
        return None

    appraisal = frappe.get_doc(
        {
            "doctype": "Appraisal",
            "company": employee.company,
            "employee": employee.name,
            "appraisal_cycle": appraisal_cycle,
            "appraisal_template": appraisal_template,
        }
    )
    appraisal.rate_goals_manually = (
        1 if cycle.kra_evaluation_method == "Manual Rating" else 0
    )
    appraisal.set_kras_and_rating_criteria()
    appraisal.flags.ignore_permissions = True
    appraisal.insert(ignore_permissions=True)
    return appraisal.name


def ensure_leave_policy_assignment(employee, rule) -> str | None:
    """Submit a Leave Policy Assignment so HRMS creates Leave Allocations."""

    employee = _employee_doc(employee)
    leave_policy = rule.get("leave_policy")
    if not employee or not leave_policy:
        return None

    assignment_based_on = rule.get("leave_assignment_based_on") or "Joining Date"
    filters: dict[str, Any] = {
        "employee": employee.name,
        "leave_policy": leave_policy,
        "assignment_based_on": assignment_based_on,
        "docstatus": ["!=", 2],
    }
    if assignment_based_on == "Leave Period":
        if not rule.get("leave_period"):
            return None
        filters["leave_period"] = rule.leave_period

    existing = frappe.get_all(
        "Leave Policy Assignment",
        filters=filters,
        fields=["name", "docstatus"],
        order_by="docstatus desc, creation desc",
        limit_page_length=1,
    )
    if existing:
        assignment = frappe.get_doc(
            "Leave Policy Assignment", existing[0].name
        )
        if cint(assignment.docstatus) == 0:
            assignment.flags.ignore_permissions = True
            assignment.submit()
        return assignment.name

    values: dict[str, Any] = {
        "doctype": "Leave Policy Assignment",
        "employee": employee.name,
        "company": employee.company,
        "leave_policy": leave_policy,
        "assignment_based_on": assignment_based_on,
        "carry_forward": cint(rule.get("carry_forward")),
    }
    if assignment_based_on == "Leave Period":
        values["leave_period"] = rule.leave_period

    assignment = frappe.get_doc(values)
    assignment.flags.ignore_permissions = True
    assignment.insert(ignore_permissions=True)
    assignment.submit()
    return assignment.name


def sync_time_trackers_from_salary_structures(
    *,
    company: str | None = None,
    reference_date=None,
) -> dict[str, int]:
    """Daily safety net for future-dated Salary Structure Assignments."""

    enabled_companies = {
        setting.company
        for setting in get_enabled_settings(company=company)
        if (
            cint(setting.enable_employee_automation)
            and cint(setting.auto_create_time_tracker)
            and setting.company
        )
    }
    if not enabled_companies:
        return {"processed": 0, "created": 0, "disabled": 0, "errors": 0}

    employees = frappe.get_all(
        "Employee",
        filters={
            "company": ["in", sorted(enabled_companies)],
            "status": "Active",
        },
        pluck="name",
        order_by="name",
        limit_page_length=0,
    )

    totals = {"processed": 0, "created": 0, "disabled": 0, "errors": 0}
    for employee in employees:
        totals["processed"] += 1
        savepoint = _savepoint_name("tracker_sync")
        frappe.db.savepoint(savepoint)
        try:
            result = sync_employee_time_tracker_mode(
                employee,
                reference_date=reference_date or nowdate(),
                allow_create=True,
            )
            totals["created"] += int(bool(result.created))
            totals["disabled"] += int(bool(result.widget_disabled))
        except Exception:
            frappe.db.rollback(save_point=savepoint)
            totals["errors"] += 1
            _log_automation_error(employee, _("Time Tracker daily sync"))

    return totals


def generate_monthly_payroll() -> list[dict[str, Any]]:
    """Create previous-month Time Tracker payroll and leave slips in Draft."""

    anchor = add_months(getdate(nowdate()), -1)
    return generate_time_tracker_payroll_for_period(
        get_first_day(anchor),
        get_last_day(anchor),
    )


def generate_monthly_time_tracker_payroll() -> list[dict[str, Any]]:
    """Backward-compatible entry point that generates only hourly payroll."""

    anchor = add_months(getdate(nowdate()), -1)
    return generate_time_tracker_payroll_for_period(
        get_first_day(anchor),
        get_last_day(anchor),
    )


def generate_monthly_fixed_salary_payroll() -> list[dict[str, Any]]:
    """Generate only the previous month's fixed-salary Payroll Entries."""

    anchor = add_months(getdate(nowdate()), -1)
    return generate_fixed_salary_payroll_for_period(
        get_first_day(anchor),
        get_last_day(anchor),
    )


def generate_payroll_for_period(
    start_date,
    end_date,
) -> list[dict[str, Any]]:
    """Generate Time Tracker and fixed-salary payroll for an explicit period."""

    return _generate_payroll_for_period(
        start_date,
        end_date,
        payroll_modes=(
            AUTOMATION_MODE_TIME_TRACKER,
            AUTOMATION_MODE_FIXED_SALARY,
        ),
    )


def generate_time_tracker_payroll_for_period(
    start_date,
    end_date,
) -> list[dict[str, Any]]:
    """Generate only Time Tracker payroll for compatibility or repair runs."""

    return _generate_payroll_for_period(
        start_date,
        end_date,
        payroll_modes=(AUTOMATION_MODE_TIME_TRACKER,),
    )


def generate_company_time_tracker_payroll_for_period(
    company: str,
    start_date,
    end_date,
) -> dict[str, Any]:
    """Generate one configured company's Time Tracker payroll immediately."""

    rule = get_company_setting(company, enabled_only=True)
    if not rule or not cint(rule.enable_auto_create_monthly_payroll):
        frappe.throw(
            _("Monthly Time Tracker payroll is not enabled for Company {0}.").format(
                frappe.bold(company)
            )
        )

    start_date = getdate(start_date)
    end_date = getdate(end_date)
    if start_date > end_date:
        frappe.throw(_("Payroll Start Date cannot be after End Date."))

    return _generate_company_time_tracker_payroll(
        rule,
        start_date=start_date,
        end_date=end_date,
    )


def generate_fixed_salary_payroll_for_period(
    start_date,
    end_date,
) -> list[dict[str, Any]]:
    """Generate only standard fixed-salary payroll for an explicit period."""

    return _generate_payroll_for_period(
        start_date,
        end_date,
        payroll_modes=(AUTOMATION_MODE_FIXED_SALARY,),
    )


def _generate_payroll_for_period(
    start_date,
    end_date,
    *,
    payroll_modes: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Run each company and payroll mode in its own transaction boundary."""

    settings = get_enabled_settings()
    if not settings:
        return []

    start_date = getdate(start_date)
    end_date = getdate(end_date)
    if start_date > end_date:
        frappe.throw(_("Payroll Start Date cannot be after End Date."))

    generators = {
        AUTOMATION_MODE_TIME_TRACKER: _generate_company_time_tracker_payroll,
        AUTOMATION_MODE_FIXED_SALARY: _generate_company_fixed_salary_payroll,
    }
    unknown_modes = [mode for mode in payroll_modes if mode not in generators]
    if unknown_modes:
        frappe.throw(
            _("Unsupported Time Tracker payroll mode: {0}").format(
                ", ".join(unknown_modes)
            )
        )

    results: list[dict[str, Any]] = []
    for rule in settings:
        if not (
            cint(rule.enabled)
            and cint(rule.enable_auto_create_monthly_payroll)
            and rule.company
        ):
            continue

        for payroll_mode in payroll_modes:
            try:
                result = generators[payroll_mode](
                    rule,
                    start_date=start_date,
                    end_date=end_date,
                )
                result.setdefault("payroll_mode", payroll_mode)
                results.append(result)

                # HRMS commits inside synchronous Salary Slip generation. Commit
                # at the same mode boundary for skipped and queued paths too, so
                # a fixed-payroll failure cannot undo completed hourly payroll.
                frappe.db.commit()
            except Exception as exc:
                frappe.db.rollback()
                _log_automation_error(
                    rule.company,
                    _("Monthly {0} Payroll").format(payroll_mode),
                )
                results.append(
                    {
                        "company": rule.company,
                        "payroll_mode": payroll_mode,
                        "payroll_entry": None,
                        "status": "Error",
                        "message": str(exc),
                        "employees": 0,
                        "salary_slips": 0,
                        "reconciled_salary_slips": 0,
                        "linked_tracker_logs": 0,
                        "linked_tracker_hours": 0.0,
                        "link_errors": 0,
                    }
                )

    return results


def _generate_company_time_tracker_payroll(
    rule,
    *,
    start_date,
    end_date,
) -> dict[str, Any]:
    # Provision trackers for hourly Assignments that became effective by the
    # end of this pay period, including Employees who joined during the month.
    sync_time_trackers_from_salary_structures(
        company=rule.company,
        reference_date=end_date,
    )
    return _generate_company_monthly_payroll(
        rule,
        start_date=start_date,
        end_date=end_date,
        payroll_mode=AUTOMATION_MODE_TIME_TRACKER,
    )


def _generate_company_fixed_salary_payroll(
    rule,
    *,
    start_date,
    end_date,
) -> dict[str, Any]:
    return _generate_company_monthly_payroll(
        rule,
        start_date=start_date,
        end_date=end_date,
        payroll_mode=AUTOMATION_MODE_FIXED_SALARY,
    )


def _generate_company_monthly_payroll(
    rule,
    *,
    start_date,
    end_date,
    payroll_mode: str,
) -> dict[str, Any]:
    _require_payroll_entry_mode_fields()

    existing = _get_existing_monthly_payroll_entry(
        rule,
        start_date=start_date,
        end_date=end_date,
        payroll_mode=payroll_mode,
    )

    if existing:
        payroll_entry = frappe.get_doc("Payroll Entry", existing.name)
        payroll_entry.flags.ignore_permissions = True

        if cint(payroll_entry.docstatus) == 0:
            _apply_monthly_payroll_configuration(
                payroll_entry,
                rule,
                start_date=start_date,
                end_date=end_date,
                payroll_mode=payroll_mode,
            )
            no_employee_message = _fill_monthly_payroll_employee_details(
                payroll_entry
            )
            if no_employee_message:
                # Persist the refreshed empty employee table on an existing
                # automated Draft entry. This prevents an old eligible list
                # from remaining visible after Salary Structure Assignments
                # change between scheduler runs.
                payroll_entry.set("employees", [])
                payroll_entry.number_of_employees = 0
                payroll_entry.save(ignore_permissions=True)
                return _skipped_payroll_result(
                    rule.company,
                    payroll_mode,
                    no_employee_message,
                    payroll_entry=payroll_entry.name,
                )

            payroll_entry.save(ignore_permissions=True)
            payroll_entry.submit()
            status = "Submitted"
        else:
            status = _complete_or_retry_salary_slip_creation(payroll_entry)

        return _build_monthly_payroll_result(
            rule.company,
            payroll_entry,
            payroll_mode=payroll_mode,
            status=status,
        )

    payroll_entry = _new_monthly_payroll_entry(
        rule,
        start_date=start_date,
        end_date=end_date,
        payroll_mode=payroll_mode,
    )
    no_employee_message = _fill_monthly_payroll_employee_details(payroll_entry)
    if no_employee_message:
        return _skipped_payroll_result(
            rule.company,
            payroll_mode,
            no_employee_message,
        )

    payroll_entry.insert(ignore_permissions=True)
    payroll_entry.flags.ignore_permissions = True
    payroll_entry.submit()

    return _build_monthly_payroll_result(
        rule.company,
        payroll_entry,
        payroll_mode=payroll_mode,
        status="Submitted",
    )


def _require_payroll_entry_mode_fields() -> None:
    required_fields = (
        "custom_pay_using_time_tracker",
        PAYROLL_ENTRY_AUTOMATION_MODE_FIELD,
    )
    missing = [
        fieldname
        for fieldname in required_fields
        if not frappe.db.has_column("Payroll Entry", fieldname)
    ]
    if missing:
        frappe.throw(
            _(
                "Run bench migrate before using monthly payroll automation. "
                "Missing Payroll Entry fields: {0}."
            ).format(", ".join(missing))
        )


def _get_existing_monthly_payroll_entry(
    rule,
    *,
    start_date,
    end_date,
    payroll_mode: str,
):
    pay_using_time_tracker = int(
        payroll_mode == AUTOMATION_MODE_TIME_TRACKER
    )
    filters = {
        "company": rule.company,
        "start_date": start_date,
        "end_date": end_date,
        "payroll_frequency": "Monthly",
        "salary_slip_based_on_timesheet": 0,
        "custom_pay_using_time_tracker": pay_using_time_tracker,
        PAYROLL_ENTRY_AUTOMATION_MODE_FIELD: payroll_mode,
        "docstatus": ["!=", 2],
    }
    rows = frappe.get_all(
        "Payroll Entry",
        filters=filters,
        fields=["name", "docstatus"],
        order_by="creation desc",
        limit_page_length=1,
    )
    if rows:
        return frappe._dict(rows[0])

    # Version 0.5.1 generated Time Tracker Payroll Entries before the automation
    # marker existed. Adopt one matching live entry rather than creating a
    # duplicate. Fixed-salary automation is new, so normal manual payroll is
    # never adopted as an automated fixed entry.
    if payroll_mode != AUTOMATION_MODE_TIME_TRACKER:
        return None

    legacy_rows = frappe.get_all(
        "Payroll Entry",
        filters={
            "company": rule.company,
            "start_date": start_date,
            "end_date": end_date,
            "payroll_frequency": "Monthly",
            "salary_slip_based_on_timesheet": 0,
            "custom_pay_using_time_tracker": 1,
            "docstatus": ["!=", 2],
        },
        fields=["name", "docstatus"],
        order_by="creation desc",
        limit_page_length=1,
    )
    if not legacy_rows:
        return None

    legacy = frappe._dict(legacy_rows[0])
    frappe.db.set_value(
        "Payroll Entry",
        legacy.name,
        PAYROLL_ENTRY_AUTOMATION_MODE_FIELD,
        AUTOMATION_MODE_TIME_TRACKER,
        update_modified=False,
    )
    return legacy


def _new_monthly_payroll_entry(
    rule,
    *,
    start_date,
    end_date,
    payroll_mode: str,
):
    payroll_entry = frappe.get_doc({"doctype": "Payroll Entry"})
    _apply_monthly_payroll_configuration(
        payroll_entry,
        rule,
        start_date=start_date,
        end_date=end_date,
        payroll_mode=payroll_mode,
    )
    payroll_entry.flags.ignore_permissions = True
    return payroll_entry


def _apply_monthly_payroll_configuration(
    payroll_entry,
    rule,
    *,
    start_date,
    end_date,
    payroll_mode: str,
) -> None:
    payroll_entry.company = rule.company
    payroll_entry.posting_date = end_date
    payroll_entry.payroll_frequency = "Monthly"
    payroll_entry.start_date = start_date
    payroll_entry.end_date = end_date
    payroll_entry.currency = rule.currency
    payroll_entry.exchange_rate = flt(rule.exchange_rate) or 1
    payroll_entry.payroll_payable_account = rule.payroll_payable_account
    payroll_entry.cost_center = rule.cost_center

    # The standard Timesheet option remains off for both generated entries.
    # Time Tracker payroll uses only Time Tracker's separate custom checkbox.
    payroll_entry.salary_slip_based_on_timesheet = 0
    payroll_entry.custom_pay_using_time_tracker = int(
        payroll_mode == AUTOMATION_MODE_TIME_TRACKER
    )
    payroll_entry.set(PAYROLL_ENTRY_AUTOMATION_MODE_FIELD, payroll_mode)


def _fill_monthly_payroll_employee_details(payroll_entry) -> str | None:
    try:
        payroll_entry.fill_employee_details()
    except frappe.ValidationError as exc:
        if not payroll_entry.get("employees"):
            return str(exc)
        raise
    return None


def _complete_or_retry_salary_slip_creation(payroll_entry) -> str:
    if payroll_entry.get("status") == "Queued":
        return "Queued"

    salary_slip_count = _count_payroll_entry_salary_slips(payroll_entry.name)
    expected_count = len(payroll_entry.get("employees") or [])
    creation_complete = (
        expected_count > 0
        and cint(payroll_entry.get("salary_slips_created"))
        and salary_slip_count >= expected_count
    )
    if creation_complete:
        return "Already Submitted"

    # Safe retry for a failed or interrupted worker. HRMS skips Salary Slips
    # already linked to this Payroll Entry.
    payroll_entry.create_salary_slips()
    return "Retried"


def _skipped_payroll_result(
    company: str,
    payroll_mode: str,
    message: str,
    *,
    payroll_entry: str | None = None,
) -> dict[str, Any]:
    return {
        "company": company,
        "payroll_mode": payroll_mode,
        "payroll_entry": payroll_entry,
        "status": "Skipped",
        "message": message,
        "employees": 0,
        "salary_slips": 0,
        "reconciled_salary_slips": 0,
        "linked_tracker_logs": 0,
        "linked_tracker_hours": 0.0,
        "link_errors": 0,
    }


def _build_monthly_payroll_result(
    company: str,
    payroll_entry,
    *,
    payroll_mode: str,
    status: str,
) -> dict[str, Any]:
    """Return Salary Slip creation and optional Tracker Log-link status."""

    link_result = frappe._dict(
        reconciled=0,
        linked_tracker_logs=0,
        linked_tracker_hours=0,
        errors=[],
    )
    if payroll_mode == AUTOMATION_MODE_TIME_TRACKER:
        link_result = reconcile_time_tracker_salary_slips_for_payroll_entry(
            payroll_entry.name,
        )

    payroll_entry.reload()
    payroll_entry_status = payroll_entry.get("status") or status
    effective_status = status
    if payroll_entry_status in {"Queued", "Failed"}:
        effective_status = payroll_entry_status

    return {
        "company": company,
        "payroll_mode": payroll_mode,
        "payroll_entry": payroll_entry.name,
        "status": effective_status,
        "payroll_entry_status": payroll_entry_status,
        "message": payroll_entry.get("error_message") or "",
        "employees": len(payroll_entry.get("employees") or []),
        "salary_slips": _count_payroll_entry_salary_slips(payroll_entry.name),
        "reconciled_salary_slips": int(link_result.reconciled or 0),
        "linked_tracker_logs": int(link_result.linked_tracker_logs or 0),
        "linked_tracker_hours": flt(
            link_result.linked_tracker_hours,
            6,
        ),
        "link_errors": len(link_result.errors or []),
    }


def _count_payroll_entry_salary_slips(payroll_entry: str) -> int:
    return frappe.db.count(
        "Salary Slip",
        {"payroll_entry": payroll_entry, "docstatus": ["!=", 2]},
    )


def _get_settings():
    """Compatibility helper returning all enabled Time Tracker Setting rows."""

    return get_enabled_settings()


def _get_company_rule(settings, company: str | None):
    del settings
    return get_company_setting(company, enabled_only=True)


def _employee_doc(employee):
    if not employee:
        return None
    if isinstance(employee, str):
        return frappe.get_doc("Employee", employee)
    return employee


def _run_employee_step(
    employee: str,
    label: str,
    callback: Callable[[], Any],
) -> Any:
    savepoint = _savepoint_name("employee")
    frappe.db.savepoint(savepoint)
    try:
        return callback()
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        _log_automation_error(employee, label)
        return None


def _savepoint_name(prefix: str) -> str:
    return f"time_tracker_{prefix}_{uuid4().hex[:10]}"


def _log_automation_error(reference: str, action: str) -> None:
    frappe.log_error(
        message=frappe.get_traceback(),
        title=f"Time Tracker {action}: {reference}",
    )
