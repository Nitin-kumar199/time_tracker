from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.query_builder import Order
from frappe.utils import add_days, cint, escape_html, flt, formatdate, getdate


TIME_TRACKER_SOURCE = "Time Tracker"
TIMESHEET_SOURCE = "Timesheet"
TRACKER_LOG_SOURCE = "Tracker Log"
PAYROLL_ENTRY_TIME_TRACKER_FIELD = "custom_pay_using_time_tracker"
PAYROLL_ENTRY_AUTOMATION_MODE_FIELD = "custom_time_tracker_automation_mode"
SALARY_STRUCTURE_TIME_TRACKER_FIELD = "custom_based_on_time_tracker"
AUTOMATION_MODE_TIME_TRACKER = "Time Tracker"
AUTOMATION_MODE_FIXED_SALARY = "Fixed Salary"
_LOG_ALLOCATION_FLAG = "time_tracker_tracker_log_names"
_EXPECTED_HOURS_ROWS_FLAG = "time_tracker_expected_hours_rows"
LEGACY_TIMESHEET_DETAIL_ALLOCATION_FIELD = "custom_time_tracker_salary_slip"

# The site's original Employee fields are authoritative. Time Tracker briefly
# introduced ``custom_working_hours_weekly_limit`` as a second field; keep it
# as a synchronised compatibility alias so existing reports and integrations
# continue to work without presenting two editable weekly-limit fields.
EMPLOYEE_HOURLY_RATE_FIELDS = (
    "custom_hourly_rate_usd",
    "custom_time_tracker_hourly_rate",
    "custom_hourly_rate",
)
EMPLOYEE_WEEKLY_LIMIT_FIELDS = (
    "custom_weekly_hours_limit",
    "custom_working_hours_weekly_limit",
)
SALARY_SLIP_WEEKLY_SUMMARY_JSON_FIELD = "custom_weekly_time_tracker_summary_json"
DEFAULT_WEEKLY_HOURS_LIMIT = 40.0
_SALARY_STRUCTURE_FLAG = "time_tracker_time_tracker_salary_structure"


def validate_salary_structure_time_tracker_mode(doc, method: str | None = None) -> None:
    """Keep Time Tracker payroll independent from ERPNext Timesheet payroll."""

    del method
    meta = getattr(doc, "meta", None)
    if not meta or not meta.has_field(SALARY_STRUCTURE_TIME_TRACKER_FIELD):
        return

    based_on_tracker = cint(doc.get(SALARY_STRUCTURE_TIME_TRACKER_FIELD))
    based_on_timesheet = cint(doc.get("salary_slip_based_on_timesheet"))
    if based_on_tracker and based_on_timesheet:
        frappe.throw(
            _(
                "Based on Time Tracker and Salary Slip Based on Timesheet are "
                "separate salary modes. Select only one."
            )
        )

    if based_on_tracker and not doc.get("salary_component"):
        frappe.throw(
            _(
                "Select an Hourly Salary Component before enabling Based on "
                "Time Tracker."
            )
        )


def uses_time_tracker_payroll_entry(doc) -> bool:
    return bool(
        doc.meta.has_field(PAYROLL_ENTRY_TIME_TRACKER_FIELD)
        and cint(doc.get(PAYROLL_ENTRY_TIME_TRACKER_FIELD))
    )


def get_payroll_entry_automation_mode(doc) -> str:
    """Return Time Tracker's read-only scheduler marker when it is available."""

    if not doc:
        return ""

    meta = getattr(doc, "meta", None)
    if meta and meta.has_field(PAYROLL_ENTRY_AUTOMATION_MODE_FIELD):
        return str(doc.get(PAYROLL_ENTRY_AUTOMATION_MODE_FIELD) or "")

    if not getattr(doc, "name", None):
        return ""

    if not frappe.db.has_column(
        "Payroll Entry",
        PAYROLL_ENTRY_AUTOMATION_MODE_FIELD,
    ):
        return ""

    return str(
        frappe.db.get_value(
            "Payroll Entry",
            doc.name,
            PAYROLL_ENTRY_AUTOMATION_MODE_FIELD,
        )
        or ""
    )


def uses_automated_fixed_salary_payroll_entry(doc) -> bool:
    """Return whether this is Time Tracker's generated fixed-salary payroll."""

    return (
        get_payroll_entry_automation_mode(doc) == AUTOMATION_MODE_FIXED_SALARY
        and not uses_time_tracker_payroll_entry(doc)
        and not cint(doc.get("salary_slip_based_on_timesheet"))
    )


def validate_time_tracker_automation_mode(doc) -> None:
    """Keep scheduler metadata consistent with the two visible payroll flags."""

    mode = get_payroll_entry_automation_mode(doc)
    if not mode:
        return

    if mode == AUTOMATION_MODE_TIME_TRACKER:
        if not uses_time_tracker_payroll_entry(doc):
            frappe.throw(
                _(
                    "An automated Time Tracker Payroll Entry must have Pay "
                    "Using Time Tracker enabled."
                )
            )
        validate_time_tracker_payroll_mode(doc)
        return

    if mode == AUTOMATION_MODE_FIXED_SALARY:
        if uses_time_tracker_payroll_entry(doc) or cint(
            doc.get("salary_slip_based_on_timesheet")
        ):
            frappe.throw(
                _(
                    "An automated Fixed Salary Payroll Entry must keep both "
                    "Pay Using Time Tracker and Salary Slip Based on "
                    "Timesheet disabled."
                )
            )
        return

    frappe.throw(
        _("Unsupported Time Tracker Automation Mode: {0}").format(
            frappe.bold(mode)
        )
    )


def validate_time_tracker_payroll_mode(doc) -> None:
    """Keep Time Tracker and Timesheet payroll as two separate choices."""

    if not uses_time_tracker_payroll_entry(doc):
        return

    if cint(doc.get("salary_slip_based_on_timesheet")):
        frappe.throw(
            _(
                "Pay Using Time Tracker and Salary Slip Based on Timesheet are "
                "separate payroll modes. Select only one."
            )
        )


def validate_time_tracker_payroll_entry(doc, method: str | None = None) -> None:
    """Validate selected employees immediately before Payroll Entry submit."""

    del method
    validate_time_tracker_payroll_mode(doc)

    if not uses_time_tracker_payroll_entry(doc):
        return

    employees = sorted(
        {
            row.employee
            for row in (doc.get("employees") or [])
            if row.get("employee")
        }
    )

    if not employees:
        frappe.throw(
            _(
                "Add at least one Employee before submitting a Time Tracker "
                "Payroll Entry. Use Get Employees after the payroll dates and "
                "payable account are set."
            ),
            title=_("Employees Required"),
        )

    existing_salary_slip_employees = (
        _get_existing_payroll_entry_salary_slip_employees(doc, employees)
    )
    employees_to_create = [
        employee
        for employee in employees
        if employee not in existing_salary_slip_employees
    ]

    # Employee onboarding normally creates the tracker. Payroll validation
    # remains read-only and reports any incomplete onboarding explicitly.

    employees_with_trackers = set(
        frappe.get_all(
            "Time Tracker",
            filters={"employee": ["in", employees]},
            pluck="employee",
            limit_page_length=0,
        )
    )
    missing = [
        employee
        for employee in employees
        if employee not in employees_with_trackers
    ]

    if missing:
        preview = _employee_preview(missing)
        frappe.throw(
            _(
                "Create a Time Tracker for these employees before submitting "
                "this Payroll Entry: {0}"
            ).format(preview),
            title=_("Time Tracker Missing"),
        )

    if employees_to_create:
        employee_dates = {
            row.name: row.date_of_joining
            for row in frappe.get_all(
                "Employee",
                filters={"name": ["in", employees_to_create]},
                fields=["name", "date_of_joining"],
                limit_page_length=0,
            )
        }
        missing_joining_date = [
            employee
            for employee in employees_to_create
            if not employee_dates.get(employee)
        ]
        if missing_joining_date:
            frappe.throw(
                _(
                    "Set Date of Joining on these Employees before creating "
                    "Salary Slips: {0}"
                ).format(_employee_preview(missing_joining_date)),
                title=_("Date of Joining Required"),
            )

    from hrms.payroll.doctype.payroll_entry.payroll_entry import get_employee_list

    eligibility_filters = frappe._dict(
        company=doc.get("company"),
        branch=doc.get("branch"),
        department=doc.get("department"),
        designation=doc.get("designation"),
        grade=doc.get("grade"),
        currency=doc.get("currency"),
        start_date=doc.get("start_date"),
        end_date=doc.get("end_date"),
        payroll_payable_account=doc.get("payroll_payable_account"),
        salary_slip_based_on_timesheet=0,
        payroll_frequency=doc.get("payroll_frequency"),
    )
    eligible_rows = get_employee_list(
        filters=eligibility_filters,
        as_dict=True,
        ignore_match_conditions=True,
    )
    eligible_rows = filter_time_tracker_payroll_employee_rows(
        eligible_rows,
        eligibility_filters,
        as_dict=True,
        validate_pay_configuration=False,
    )
    eligible_employees = {
        row.employee
        for row in eligible_rows
    }
    ineligible = [
        employee
        for employee in employees_to_create
        if employee not in eligible_employees
    ]
    if ineligible:
        frappe.throw(
            _(
                "These selected Employees no longer match the Payroll Entry "
                "company, dates, hourly Salary Structure Assignment, Payroll "
                "Payable Account, or existing-payroll checks: {0}. Run Get "
                "Employees again."
            ).format(_employee_preview(ineligible)),
            title=_("Time Tracker Payroll Employee Not Eligible"),
        )

    assignments = get_time_tracker_payroll_assignment_map(
        employees=employees_to_create,
        start_date=doc.get("start_date"),
        end_date=doc.get("end_date"),
        company=doc.get("company"),
        currency=doc.get("currency"),
        payroll_payable_account=doc.get("payroll_payable_account"),
    )

    missing_structure: list[str] = []
    missing_component: list[str] = []
    missing_rate: list[str] = []

    for employee in employees_to_create:
        structure = assignments.get(employee)

        if not structure:
            missing_structure.append(employee)
            continue

        if not structure.get("salary_component"):
            missing_component.append(
                _("{0} ({1})").format(employee, structure.salary_structure)
            )

        settings = get_employee_time_tracker_pay_settings(
            employee,
            currency=doc.get("currency"),
        )
        if flt(settings.hourly_rate) <= 0 and flt(structure.hour_rate) <= 0:
            missing_rate.append(
                _("{0} ({1})").format(employee, structure.salary_structure)
            )

    if missing_structure:
        frappe.throw(
            _(
                "These Employees do not have a submitted, active hourly Salary "
                "Structure Assignment for the Payroll Entry company, currency, "
                "dates, and Payroll Payable Account: {0}"
            ).format(_employee_preview(missing_structure)),
            title=_("Hourly Salary Structure Assignment Missing"),
        )

    if missing_component:
        frappe.throw(
            _(
                "Set an Hourly Salary Component on these hourly Salary "
                "Structures: {0}"
            ).format(_employee_preview(missing_component)),
            title=_("Hourly Salary Component Required"),
        )

    if missing_rate:
        frappe.throw(
            _(
                "Set a positive Employee custom_hourly_rate_usd (preferred), "
                "a positive Time Tracker Hourly Rate alias, or a positive Hour "
                "Rate on the assigned Salary Structure for: {0}"
            ).format(_employee_preview(missing_rate)),
            title=_("Hourly Rate Required"),
        )


def validate_automated_fixed_salary_payroll_entry(doc) -> None:
    """Validate employees selected by the generated fixed-salary payroll.

    HRMS's candidate query can see an older fixed Assignment even when a newer
    hourly Assignment is already effective. The automation applies the same
    latest-effective Assignment resolver used by Time Tracker payroll so an
    Employee can never be present in both generated Payroll Entries.
    """

    validate_time_tracker_automation_mode(doc)
    if not uses_automated_fixed_salary_payroll_entry(doc):
        return

    employees = sorted(
        {
            row.employee
            for row in (doc.get("employees") or [])
            if row.get("employee")
        }
    )
    if not employees:
        frappe.throw(
            _(
                "Add at least one Employee before submitting an automated "
                "Fixed Salary Payroll Entry."
            ),
            title=_("Employees Required"),
        )

    assignments = get_fixed_salary_payroll_assignment_map(
        employees=employees,
        start_date=doc.get("start_date"),
        end_date=doc.get("end_date"),
        company=doc.get("company"),
        currency=doc.get("currency"),
        payroll_payable_account=doc.get("payroll_payable_account"),
        payroll_frequency=doc.get("payroll_frequency"),
    )
    ineligible = [employee for employee in employees if employee not in assignments]
    if ineligible:
        frappe.throw(
            _(
                "These selected Employees no longer have a latest effective "
                "submitted fixed Salary Structure Assignment matching the "
                "Payroll Entry company, currency, frequency, dates, and "
                "Payroll Payable Account: {0}. Run Get Employees again."
            ).format(_employee_preview(ineligible)),
            title=_("Fixed Salary Employee Not Eligible"),
        )


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def time_tracker_employee_query(
    doctype,
    txt,
    searchfield,
    start,
    page_len,
    filters,
):
    """Return hourly-payroll Employees that also have a Time Tracker."""

    del doctype

    from hrms.payroll.doctype.payroll_entry.payroll_entry import get_employee_list

    parsed_filters = (
        frappe.parse_json(filters)
        if isinstance(filters, str)
        else (filters or {})
    )
    filters = frappe._dict(parsed_filters or {})
    filters.salary_slip_based_on_timesheet = 0

    if not filters.get("payroll_frequency"):
        frappe.throw(_("Select Payroll Frequency."))

    # HRMS returns two-item rows when ``as_dict=False``.  Load the complete
    # permitted hourly-employee result, apply the Time Tracker requirement, and
    # paginate only after filtering.
    rows = get_employee_list(
        filters,
        searchfield=searchfield,
        search_string=txt,
        fields=["name", "employee_name"],
        as_dict=False,
        limit=None,
        offset=None,
    )

    if not rows:
        return []

    matching_rows = filter_time_tracker_payroll_employee_rows(
        rows,
        filters,
        as_dict=False,
    )

    start = max(cint(start), 0)
    page_len = max(cint(page_len), 1)
    return matching_rows[start : start + page_len]


def get_time_tracker_payroll_assignment_map(
    *,
    employees: list[str] | tuple[str, ...] | set[str],
    start_date,
    end_date,
    company: str | None,
    currency: str | None,
    payroll_payable_account: str | None = None,
) -> dict[str, frappe._dict]:
    """Return each Employee's latest applicable assignment when it is hourly."""

    return _get_payroll_assignment_map(
        employees=employees,
        start_date=start_date,
        end_date=end_date,
        company=company,
        currency=currency,
        payroll_payable_account=payroll_payable_account,
        salary_slip_based_on_timesheet=0,
        based_on_time_tracker=1,
    )


def get_fixed_salary_payroll_assignment_map(
    *,
    employees: list[str] | tuple[str, ...] | set[str],
    start_date,
    end_date,
    company: str | None,
    currency: str | None,
    payroll_payable_account: str | None = None,
    payroll_frequency: str | None = None,
) -> dict[str, frappe._dict]:
    """Return each Employee's latest applicable fixed Salary Structure.

    The latest Assignment effective on the Employee's actual Salary Slip start
    is authoritative. An older fixed Assignment is not used when a newer hourly
    Assignment is already effective, which keeps the two automated Payroll
    Entries disjoint.
    """

    return _get_payroll_assignment_map(
        employees=employees,
        start_date=start_date,
        end_date=end_date,
        company=company,
        currency=currency,
        payroll_payable_account=payroll_payable_account,
        salary_slip_based_on_timesheet=0,
        based_on_time_tracker=0,
        payroll_frequency=payroll_frequency,
    )


def _get_payroll_assignment_map(
    *,
    employees: list[str] | tuple[str, ...] | set[str],
    start_date,
    end_date,
    company: str | None,
    currency: str | None,
    payroll_payable_account: str | None,
    salary_slip_based_on_timesheet: int,
    based_on_time_tracker: int | None = None,
    payroll_frequency: str | None = None,
) -> dict[str, frappe._dict]:
    """Resolve the latest applicable Assignment for one payroll mode.

    HRMS initially finds Payroll Entry candidates using Assignments whose
    ``from_date`` is on or before the period end. Salary Slip validation is
    stricter: the selected Assignment must be effective on or before the
    Employee's actual slip start (the period start, or a later joining date).
    Applying the stricter rule here prevents an Employee from being fetched by
    both the hourly and fixed automation runs or failing during slip creation.
    """

    employee_names = sorted({employee for employee in employees if employee})
    if not (
        employee_names
        and start_date
        and end_date
        and company
        and currency
    ):
        return {}

    period_start = getdate(start_date)
    period_end = getdate(end_date)
    if period_start > period_end:
        return {}

    employee_rows = frappe.get_all(
        "Employee",
        filters={
            "name": ["in", employee_names],
            "company": company,
            "status": ["!=", "Inactive"],
        },
        fields=["name", "date_of_joining"],
        limit_page_length=0,
    )
    actual_start_dates: dict[str, Any] = {}
    for employee_row in employee_rows:
        # HRMS's Payroll Entry candidate query accepts a blank Date of Joining,
        # but Salary Slip.validate_dates() rejects it later. Exclude it here so
        # Get Employees and Salary Slip creation share the same requirement.
        if not employee_row.date_of_joining:
            continue

        joining_date = getdate(employee_row.date_of_joining)
        actual_start = period_start
        if period_start < joining_date <= period_end:
            actual_start = joining_date
        elif joining_date > period_end:
            continue

        actual_start_dates[employee_row.name] = actual_start

    if not actual_start_dates:
        return {}

    SalaryStructureAssignment = frappe.qb.DocType(
        "Salary Structure Assignment"
    )
    SalaryStructure = frappe.qb.DocType("Salary Structure")

    query = (
        frappe.qb.from_(SalaryStructureAssignment)
        .inner_join(SalaryStructure)
        .on(
            SalaryStructureAssignment.salary_structure
            == SalaryStructure.name
        )
        .select(
            SalaryStructureAssignment.employee,
            SalaryStructureAssignment.name.as_("assignment"),
            SalaryStructureAssignment.salary_structure,
            SalaryStructureAssignment.from_date,
            SalaryStructureAssignment.payroll_payable_account,
            SalaryStructure.hour_rate,
            SalaryStructure.salary_component,
            SalaryStructure.company,
            SalaryStructure.currency,
            SalaryStructure.payroll_frequency,
            SalaryStructure.salary_slip_based_on_timesheet,
            SalaryStructure.custom_based_on_time_tracker,
        )
        .where(
            (SalaryStructureAssignment.docstatus == 1)
            & (SalaryStructure.docstatus == 1)
            & (SalaryStructure.is_active == "Yes")
            & (SalaryStructureAssignment.employee.isin(employee_names))
            & (SalaryStructureAssignment.from_date <= period_end)
        )
        .orderby(SalaryStructureAssignment.employee)
        .orderby(
            SalaryStructureAssignment.from_date,
            order=Order.desc,
        )
        .orderby(
            SalaryStructureAssignment.modified,
            order=Order.desc,
        )
    )

    return _resolve_payroll_assignments(
        query.run(as_dict=True),
        actual_start_dates=actual_start_dates,
        company=company,
        currency=currency,
        payroll_payable_account=payroll_payable_account,
        salary_slip_based_on_timesheet=salary_slip_based_on_timesheet,
        based_on_time_tracker=based_on_time_tracker,
        payroll_frequency=payroll_frequency,
    )


def _resolve_payroll_assignments(
    rows: list[dict] | list[frappe._dict],
    *,
    actual_start_dates: dict[str, Any],
    company: str,
    currency: str,
    payroll_payable_account: str | None,
    salary_slip_based_on_timesheet: int,
    based_on_time_tracker: int | None = None,
    payroll_frequency: str | None = None,
) -> dict[str, frappe._dict]:
    """Resolve ordered Assignment rows without falling back past a mismatch.

    ``rows`` must be ordered by Employee and descending Assignment From Date.
    Assignments that begin after the Employee's actual Salary Slip start are
    skipped. The first applicable row is authoritative: when it belongs to the
    other payroll mode, another company/currency, another payable account, or a
    different fixed payroll frequency, an older row must not be selected.
    """

    assignments: dict[str, frappe._dict] = {}
    resolved_employees: set[str] = set()
    expected_mode = cint(salary_slip_based_on_timesheet)
    expected_time_tracker = (
        None if based_on_time_tracker is None else cint(based_on_time_tracker)
    )

    for raw_row in rows:
        row = frappe._dict(raw_row)
        if row.employee in resolved_employees:
            continue

        actual_start = actual_start_dates.get(row.employee)
        if not actual_start or getdate(row.from_date) > actual_start:
            continue

        resolved_employees.add(row.employee)
        frequency_matches = bool(
            expected_mode
            or not payroll_frequency
            or row.get("payroll_frequency") == payroll_frequency
        )
        time_tracker_matches = bool(
            expected_time_tracker is None
            or cint(row.get(SALARY_STRUCTURE_TIME_TRACKER_FIELD))
            == expected_time_tracker
        )
        if (
            cint(row.salary_slip_based_on_timesheet) == expected_mode
            and time_tracker_matches
            and row.company == company
            and row.currency == currency
            and frequency_matches
            and (
                not payroll_payable_account
                or row.payroll_payable_account == payroll_payable_account
            )
        ):
            assignments[row.employee] = row

    return assignments


def _resolve_time_tracker_payroll_assignments(
    rows: list[dict] | list[frappe._dict],
    *,
    actual_start_dates: dict[str, Any],
    company: str,
    currency: str,
    payroll_payable_account: str | None,
) -> dict[str, frappe._dict]:
    """Compatibility wrapper for hourly-assignment unit tests/integrations."""

    return _resolve_payroll_assignments(
        rows,
        actual_start_dates=actual_start_dates,
        company=company,
        currency=currency,
        payroll_payable_account=payroll_payable_account,
        salary_slip_based_on_timesheet=0,
        based_on_time_tracker=1,
    )


def _resolve_fixed_salary_payroll_assignments(
    rows: list[dict] | list[frappe._dict],
    *,
    actual_start_dates: dict[str, Any],
    company: str,
    currency: str,
    payroll_payable_account: str | None,
    payroll_frequency: str | None,
) -> dict[str, frappe._dict]:
    """Resolve fixed-salary Assignments for automation and unit tests."""

    return _resolve_payroll_assignments(
        rows,
        actual_start_dates=actual_start_dates,
        company=company,
        currency=currency,
        payroll_payable_account=payroll_payable_account,
        salary_slip_based_on_timesheet=0,
        based_on_time_tracker=0,
        payroll_frequency=payroll_frequency,
    )


def filter_time_tracker_payroll_employee_rows(
    rows: list,
    filters,
    *,
    as_dict: bool,
    validate_pay_configuration: bool = True,
) -> list:
    """Keep only rows that can create a valid Time Tracker Salary Slip."""

    if not rows:
        return []

    filters = frappe._dict(filters or {})
    employee_names = [
        row.get("employee") if as_dict else (row[0] if row else None)
        for row in rows
    ]
    employee_names = [employee for employee in employee_names if employee]
    if not employee_names:
        return []

    assignments = get_time_tracker_payroll_assignment_map(
        employees=employee_names,
        start_date=filters.get("start_date"),
        end_date=filters.get("end_date"),
        company=filters.get("company"),
        currency=filters.get("currency"),
        payroll_payable_account=filters.get("payroll_payable_account"),
    )
    tracker_employees = set(
        frappe.get_all(
            "Time Tracker",
            filters={"employee": ["in", employee_names]},
            pluck="employee",
            limit_page_length=0,
        )
    )

    result = []
    for row in rows:
        employee = row.get("employee") if as_dict else (row[0] if row else None)
        structure = assignments.get(employee)
        if not structure or employee not in tracker_employees:
            continue

        if validate_pay_configuration:
            settings = get_employee_time_tracker_pay_settings(
                employee,
                currency=filters.get("currency"),
            )
            if not structure.get("salary_component"):
                continue
            if (
                flt(settings.hourly_rate) <= 0
                and flt(structure.get("hour_rate")) <= 0
            ):
                continue

        result.append(row if as_dict else list(row))

    return result


def filter_fixed_salary_payroll_employee_rows(
    rows: list,
    filters,
    *,
    as_dict: bool,
) -> list:
    """Keep only Employees whose latest effective Assignment is fixed salary."""

    if not rows:
        return []

    filters = frappe._dict(filters or {})
    employee_names = [
        row.get("employee") if as_dict else (row[0] if row else None)
        for row in rows
    ]
    employee_names = [employee for employee in employee_names if employee]
    if not employee_names:
        return []

    assignments = get_fixed_salary_payroll_assignment_map(
        employees=employee_names,
        start_date=filters.get("start_date"),
        end_date=filters.get("end_date"),
        company=filters.get("company"),
        currency=filters.get("currency"),
        payroll_payable_account=filters.get("payroll_payable_account"),
        payroll_frequency=filters.get("payroll_frequency"),
    )

    result = []
    for row in rows:
        employee = row.get("employee") if as_dict else (row[0] if row else None)
        if employee in assignments:
            result.append(row if as_dict else list(row))

    return result


def get_assigned_time_tracker_salary_structure(
    *,
    employee: str,
    start_date,
    end_date,
    company: str | None,
    currency: str | None,
    payroll_payable_account: str | None = None,
) -> frappe._dict | None:
    """Return the applicable submitted hourly Salary Structure Assignment.

    HRMS's normal Salary Slip selector skips the hourly flag when the slip is
    already in timesheet mode. An Employee with a newer fixed Salary Structure
    can therefore receive that fixed structure by mistake. This selector keeps
    the Employee query and Salary Slip calculation on the same explicit hourly
    assignment, including Payroll Payable Account compatibility.
    """

    assignments = get_time_tracker_payroll_assignment_map(
        employees=[employee],
        start_date=start_date,
        end_date=end_date,
        company=company,
        currency=currency,
        payroll_payable_account=payroll_payable_account,
    )
    return assignments.get(employee)


def uses_automated_fixed_salary_slip(doc) -> bool:
    """Return whether a Salary Slip belongs to Time Tracker's fixed payroll run."""

    payroll_entry = doc.get("payroll_entry")
    if not payroll_entry or not frappe.db.has_column(
        "Payroll Entry",
        PAYROLL_ENTRY_AUTOMATION_MODE_FIELD,
    ):
        return False

    mode = frappe.db.get_value(
        "Payroll Entry",
        payroll_entry,
        PAYROLL_ENTRY_AUTOMATION_MODE_FIELD,
    )
    return mode == AUTOMATION_MODE_FIXED_SALARY


def get_assigned_fixed_salary_structure(doc) -> frappe._dict | None:
    """Return the fixed Assignment selected by the automated Payroll Entry."""

    if not (
        doc.get("employee")
        and doc.get("start_date")
        and doc.get("end_date")
        and doc.get("company")
        and doc.get("currency")
    ):
        return None

    assignments = get_fixed_salary_payroll_assignment_map(
        employees=[doc.employee],
        start_date=doc.start_date,
        end_date=doc.end_date,
        company=doc.company,
        currency=doc.currency,
        payroll_payable_account=_get_salary_slip_payroll_payable_account(doc),
        payroll_frequency=doc.get("payroll_frequency"),
    )
    return assignments.get(doc.employee)


def select_automated_fixed_salary_structure(doc) -> str:
    """Force the same fixed Assignment used while fetching Employees."""

    assignment = get_assigned_fixed_salary_structure(doc)
    if not assignment:
        frappe.throw(
            _(
                "Employee {0} no longer has an eligible fixed Salary "
                "Structure Assignment for this automated Payroll Entry."
            ).format(frappe.bold(doc.get("employee"))),
            title=_("Fixed Salary Structure Assignment Missing"),
        )

    doc.salary_structure = assignment.salary_structure
    doc.salary_slip_based_on_timesheet = 0
    return assignment.salary_structure


def initialise_time_tracker_salary_slip(doc, *, repair_draft: bool = True):
    """Set the correct hourly source/structure before HRMS validates the slip.

    Existing draft slips produced by the previous selector bug are repaired by
    clearing only their generated structure tables. HRMS then rebuilds those
    tables from the correct hourly Salary Structure during the same save.
    """

    source = get_time_source(doc)
    _set_if_available(doc, "custom_time_tracking_source", source)

    if source != TIME_TRACKER_SOURCE:
        return None

    doc.salary_slip_based_on_timesheet = 1

    if not (
        doc.get("employee")
        and doc.get("start_date")
        and doc.get("end_date")
        and doc.get("company")
        and doc.get("currency")
    ):
        return None

    payroll_payable_account = _get_salary_slip_payroll_payable_account(doc)
    current_structure = doc.get("salary_structure")
    structure = get_assigned_time_tracker_salary_structure(
        employee=doc.employee,
        start_date=doc.actual_start_date,
        end_date=doc.end_date,
        company=doc.company,
        currency=doc.currency,
        payroll_payable_account=payroll_payable_account,
    )

    if not structure:
        account_detail = (
            _(" and Payroll Payable Account {0}").format(
                frappe.bold(payroll_payable_account)
            )
            if payroll_payable_account
            else ""
        )
        frappe.throw(
            _(
                "Employee {0} has no submitted, active hourly Salary "
                "Structure Assignment for {1}, currency {2}, effective on or "
                "before {3}{4}."
            ).format(
                frappe.bold(doc.employee),
                frappe.bold(doc.company),
                frappe.bold(doc.currency),
                frappe.bold(formatdate(doc.actual_start_date)),
                account_detail,
            ),
            title=_("Hourly Salary Structure Assignment Missing"),
        )

    _validate_time_tracker_salary_structure(doc, structure)
    setattr(doc.flags, _SALARY_STRUCTURE_FLAG, structure)

    wrong_structure = current_structure != structure.salary_structure
    generated_tables_loaded = bool(
        (doc.get("earnings") or []) or (doc.get("deductions") or [])
    )

    if wrong_structure and generated_tables_loaded:
        if not repair_draft or cint(doc.get("docstatus")) != 0:
            frappe.throw(
                _(
                    "Salary Structure {0} is not a valid hourly assignment for "
                    "this Time Tracker Salary Slip. The applicable structure is {1}."
                ).format(
                    frappe.bold(current_structure or _("No Salary Structure")),
                    frappe.bold(structure.salary_structure),
                )
            )

        doc.set("earnings", [])
        doc.set("deductions", [])
        if doc.meta.has_field("loans"):
            doc.set("loans", [])
        doc.set("timesheets", [])

        for attribute in (
            "_salary_structure_doc",
            "_salary_structure_assignment",
        ):
            if hasattr(doc, attribute):
                delattr(doc, attribute)

        frappe.msgprint(
            _(
                "This draft used Salary Structure {0}. It was rebuilt with the "
                "applicable hourly Salary Structure {1}. Review the amounts "
                "before submitting."
            ).format(
                frappe.bold(current_structure or _("No Salary Structure")),
                frappe.bold(structure.salary_structure),
            ),
            title=_("Time Tracker Salary Slip Repaired"),
            indicator="blue",
        )

    doc.salary_structure = structure.salary_structure
    return structure


def select_time_tracker_salary_structure(doc) -> str:
    """Select and return the explicit hourly structure for HRMS check_sal_struct."""

    structure = initialise_time_tracker_salary_slip(doc, repair_draft=True)
    if not structure:
        frappe.throw(_("Unable to select a Time Tracker Salary Structure."))

    doc.salary_structure = structure.salary_structure
    return structure.salary_structure


def set_time_tracker_salary_structure_assignment(doc) -> None:
    """Bind HRMS calculations to the exact validated Assignment record."""

    structure = getattr(doc.flags, _SALARY_STRUCTURE_FLAG, None)
    if not structure:
        structure = initialise_time_tracker_salary_slip(
            doc,
            repair_draft=True,
        )

    assignment_name = structure.get("assignment") if structure else None
    current_structure = get_assigned_time_tracker_salary_structure(
        employee=doc.employee,
        start_date=doc.actual_start_date,
        end_date=doc.end_date,
        company=doc.company,
        currency=doc.currency,
        payroll_payable_account=_get_salary_slip_payroll_payable_account(doc),
    )
    if not current_structure or current_structure.get("assignment") != assignment_name:
        frappe.throw(
            _(
                "The applicable Salary Structure Assignment changed while this "
                "Salary Slip was being calculated. Reload and try again."
            ),
            title=_("Salary Structure Assignment Changed"),
        )

    assignment = (
        frappe.db.get_value(
            "Salary Structure Assignment",
            assignment_name,
            "*",
            as_dict=True,
        )
        if assignment_name
        else None
    )
    if not assignment:
        frappe.throw(
            _(
                "The validated hourly Salary Structure Assignment is no longer "
                "available. Reload the Salary Slip and try again."
            ),
            title=_("Salary Structure Assignment Changed"),
        )

    assignment = frappe._dict(assignment)
    if (
        cint(assignment.docstatus) != 1
        or assignment.employee != doc.employee
        or assignment.salary_structure != doc.salary_structure
        or getdate(assignment.from_date) > getdate(doc.actual_start_date)
        or assignment.payroll_payable_account
        != structure.get("payroll_payable_account")
    ):
        frappe.throw(
            _(
                "The hourly Salary Structure Assignment changed while this "
                "Salary Slip was being calculated. Reload and try again."
            ),
            title=_("Salary Structure Assignment Changed"),
        )

    doc._salary_structure_assignment = assignment


def build_time_tracker_payroll_diagnostics(doc) -> frappe._dict:
    """Return an actionable setup report matching HRMS employee eligibility."""

    required = [
        fieldname
        for fieldname in (
            "company",
            "currency",
            "payroll_frequency",
            "start_date",
            "end_date",
            "payroll_payable_account",
        )
        if not doc.get(fieldname)
    ]
    if required:
        labels = ", ".join(
            escape_html(
                (
                    doc.meta.get_field(fieldname).label
                    if doc.meta.get_field(fieldname)
                    else fieldname
                )
                or fieldname
            )
            for fieldname in required
        )
        return frappe._dict(
            ok=False,
            eligible_count=0,
            html=_("Complete these fields first: {0}.").format(labels),
        )

    from hrms.payroll.doctype.payroll_entry.payroll_entry import get_employee_list

    filters = frappe._dict(
        company=doc.company,
        branch=doc.get("branch"),
        department=doc.get("department"),
        designation=doc.get("designation"),
        grade=doc.get("grade"),
        currency=doc.currency,
        start_date=doc.start_date,
        end_date=doc.end_date,
        payroll_payable_account=doc.payroll_payable_account,
        salary_slip_based_on_timesheet=0,
        payroll_frequency=doc.get("payroll_frequency"),
    )
    hrms_candidate_rows = get_employee_list(
        filters=filters,
        as_dict=True,
        ignore_match_conditions=True,
    )
    eligible_rows = filter_time_tracker_payroll_employee_rows(
        hrms_candidate_rows,
        filters,
        as_dict=True,
    )

    structures = frappe.get_all(
        "Salary Structure",
        filters={
            "docstatus": 1,
            "is_active": "Yes",
            "company": doc.company,
            "currency": doc.currency,
            "salary_slip_based_on_timesheet": 0,
            SALARY_STRUCTURE_TIME_TRACKER_FIELD: 1,
        },
        fields=["name", "hour_rate", "salary_component"],
        order_by="name",
        limit_page_length=0,
    )

    employee_filters: dict[str, Any] = {
        "company": doc.company,
        "status": ["!=", "Inactive"],
    }
    for fieldname in ("branch", "department", "designation", "grade"):
        if doc.get(fieldname):
            employee_filters[fieldname] = doc.get(fieldname)

    company_employees = frappe.get_all(
        "Employee",
        filters=employee_filters,
        fields=["name", "date_of_joining", "relieving_date"],
        limit_page_length=0,
    )
    period_employees = [
        row
        for row in company_employees
        if (
            not row.date_of_joining
            or getdate(row.date_of_joining) <= getdate(doc.end_date)
        )
        and (
            not row.relieving_date
            or getdate(row.relieving_date) >= getdate(doc.start_date)
        )
    ]
    assignment_map = get_time_tracker_payroll_assignment_map(
        employees=[row.name for row in period_employees],
        start_date=doc.start_date,
        end_date=doc.end_date,
        company=doc.company,
        currency=doc.currency,
        payroll_payable_account=doc.payroll_payable_account,
    )
    assigned_employees = set(assignment_map)
    configured_employees: set[str] = set()
    missing_hourly_component: list[str] = []
    missing_hourly_rate: list[str] = []

    for employee, structure in assignment_map.items():
        if not structure.get("salary_component"):
            missing_hourly_component.append(
                _("{0} ({1})").format(employee, structure.salary_structure)
            )
            continue

        settings = get_employee_time_tracker_pay_settings(
            employee,
            currency=doc.currency,
        )
        if flt(settings.hourly_rate) <= 0 and flt(structure.get("hour_rate")) <= 0:
            missing_hourly_rate.append(
                _("{0} ({1})").format(employee, structure.salary_structure)
            )
            continue

        configured_employees.add(employee)

    tracker_employees = set()
    if assigned_employees:
        tracker_employees = set(
            frappe.get_all(
                "Time Tracker",
                filters={"employee": ["in", sorted(assigned_employees)]},
                pluck="employee",
                limit_page_length=0,
            )
        )

    already_paid = frappe.get_all(
        "Salary Slip",
        filters={
            "employee": ["in", [row.name for row in period_employees] or [""]],
            "start_date": doc.start_date,
            "end_date": doc.end_date,
            "docstatus": 1,
        },
        pluck="employee",
        limit_page_length=0,
    )

    missing_joining_date = [
        row.name for row in period_employees if not row.date_of_joining
    ]

    checks = [
        (_("Employees matching company/date filters"), len(period_employees)),
        (_("Employees with an effective hourly Salary Structure"), len(assigned_employees)),
        (_("Active submitted hourly Salary Structures"), len(structures)),
        (_("HRMS hourly/account candidates"), len(hrms_candidate_rows)),
        (
            _("Assignments effective by each Employee's actual period start"),
            len(assigned_employees),
        ),
        (_("Assignments with a valid component and rate"), len(configured_employees)),
        (_("Assigned Employees with a permanent Time Tracker"), len(tracker_employees)),
        (_("Already submitted for this exact period"), len(set(already_paid))),
        (_("Final Employees safe to fetch"), len(eligible_rows)),
    ]
    check_html = "".join(
        "<li><strong>{0}</strong>: {1}</li>".format(
            escape_html(label),
            cint(count),
        )
        for label, count in checks
    )

    notes: list[str] = []
    if not structures:
        notes.append(
            _(
                "Create and submit a Salary Structure for this company/currency "
                "with Based on Time Tracker enabled and the standard "
                "Salary Slip Based on Timesheet option disabled."
            )
        )
    if missing_joining_date:
        notes.append(
            _(
                "Set Date of Joining on these Employees before payroll: {0}."
            ).format(_employee_preview(missing_joining_date))
        )
    if structures and not assigned_employees and not missing_joining_date:
        notes.append(
            _(
                "Submit a Salary Structure Assignment for each Employee and set "
                "its Payroll Payable Account to the same account as this Payroll "
                "Entry. Its From Date must be on or before the Employee's actual "
                "period start (or joining date for a new joiner)."
            )
        )
    if (
        hrms_candidate_rows
        and len(assigned_employees) < len(hrms_candidate_rows)
        and not missing_joining_date
    ):
        notes.append(
            _(
                "Some HRMS candidates have an assignment that starts after their "
                "actual Salary Slip start. Move the assignment From Date earlier, "
                "or process that Employee in the next applicable payroll period."
            )
        )
    if missing_hourly_component:
        notes.append(
            _(
                "Set an Hourly Salary Component on the assigned Salary "
                "Structure for: {0}."
            ).format(_employee_preview(missing_hourly_component))
        )
    if missing_hourly_rate:
        notes.append(
            _(
                "Set Employee custom_hourly_rate_usd, a supported Employee rate "
                "alias, or the assigned Salary Structure Hour Rate for: {0}."
            ).format(_employee_preview(missing_hourly_rate))
        )
    missing_tracker_employees = sorted(assigned_employees - tracker_employees)
    if missing_tracker_employees:
        notes.append(
            _(
                "Employee onboarding did not create a permanent Time Tracker for "
                "these Employees. Review Time Tracker Setting or create the tracker "
                "before using Get Employees: {0}."
            ).format(_employee_preview(missing_tracker_employees))
        )
    if cint(doc.get("validate_attendance")):
        notes.append(
            _(
                "Validate Attendance is enabled. Unmarked attendance can still "
                "block Payroll Entry submission even when Time Tracker setup is valid."
            )
        )
    ready_before_existing = configured_employees & tracker_employees
    already_paid_employees = set(already_paid)
    if (
        not eligible_rows
        and ready_before_existing
        and ready_before_existing.issubset(already_paid_employees)
    ):
        notes.append(
            _(
                "All otherwise eligible Employees already have a submitted Salary "
                "Slip for this exact period. Use a different period or review the "
                "existing Salary Slips."
            )
        )
    elif eligible_rows:
        notes.append(
            _(
                "The setup is ready. Get Employees should fetch {0} Employee(s)."
            ).format(len(eligible_rows))
        )
    elif not notes:
        notes.append(
            _(
                "Check joining/relieving dates and whether a submitted Salary Slip "
                "already exists for this exact period."
            )
        )

    note_html = "".join(
        "<li>{0}</li>".format(note) for note in notes
    )
    html = (
        '<div class="time_tracker-payroll-diagnostics">'
        '<p>{0}</p><ul>{1}</ul><p><strong>{2}</strong></p><ul>{3}</ul></div>'
    ).format(
        _("Time Tracker payroll eligibility check"),
        check_html,
        _("Next action"),
        note_html,
    )

    return frappe._dict(
        ok=bool(eligible_rows),
        eligible_count=len(eligible_rows),
        html=html,
    )


def _validate_time_tracker_salary_structure(doc, structure) -> None:
    if not structure.get("salary_component"):
        frappe.throw(
            _(
                "Salary Structure {0} is marked for hourly payroll but has no "
                "Hourly Salary Component. Set the component and submit/save the "
                "structure before creating Salary Slips."
            ).format(frappe.bold(structure.salary_structure)),
            title=_("Hourly Salary Component Required"),
        )

    settings = get_employee_time_tracker_pay_settings(
        doc.get("employee"),
        currency=doc.get("currency"),
    )
    if flt(settings.hourly_rate) <= 0 and flt(structure.get("hour_rate")) <= 0:
        frappe.throw(
            _(
                "Set a positive custom_hourly_rate_usd on Employee {0}, or "
                "set a positive Hour Rate on Salary Structure {1}."
            ).format(
                frappe.bold(doc.get("employee") or ""),
                frappe.bold(structure.salary_structure),
            ),
            title=_("Hourly Rate Required"),
        )


def _get_salary_slip_payroll_payable_account(doc) -> str | None:
    payroll_entry = doc.get("payroll_entry")
    if not payroll_entry:
        return None

    return frappe.db.get_value(
        "Payroll Entry",
        payroll_entry,
        "payroll_payable_account",
    )


def _get_existing_payroll_entry_salary_slip_employees(
    doc,
    employees: list[str],
) -> set[str]:
    """Return live slips already created by this exact Payroll Entry.

    Payroll Entry creation can partially succeed before another Employee fails.
    Retrying must validate only Employees that still need a slip; otherwise
    HRMS's normal removal of already-submitted Employees makes a safe retry look
    ineligible and blocks the remaining work.
    """

    if not (
        employees
        and doc.get("name")
        and not str(doc.name).startswith("new-")
        and doc.get("start_date")
        and doc.get("end_date")
    ):
        return set()

    return set(
        frappe.get_all(
            "Salary Slip",
            filters={
                "payroll_entry": doc.name,
                "employee": ["in", employees],
                "start_date": doc.start_date,
                "end_date": doc.end_date,
                "docstatus": ["!=", 2],
            },
            pluck="employee",
            limit_page_length=0,
        )
    )


def _employee_preview(values: list[str], limit: int = 20) -> str:
    preview = ", ".join(
        frappe.bold(escape_html(str(value)))
        for value in values[:limit]
    )
    remainder = len(values) - limit
    if remainder > 0:
        preview += _(" and {0} more").format(remainder)
    return preview


def get_time_source(doc) -> str:
    """Resolve a Salary Slip's hourly source without using the Timesheet UI."""

    stored_source = doc.get("custom_time_tracking_source") or ""
    payroll_entry = doc.get("payroll_entry")

    if payroll_entry:
        if frappe.db.has_column(
            "Payroll Entry",
            PAYROLL_ENTRY_TIME_TRACKER_FIELD,
        ) and cint(
            frappe.db.get_value(
                "Payroll Entry",
                payroll_entry,
                PAYROLL_ENTRY_TIME_TRACKER_FIELD,
            )
        ):
            return TIME_TRACKER_SOURCE

        # Compatibility with the earlier selector-based revision while a site
        # is being migrated to the independent checkbox.
        if frappe.db.has_column("Payroll Entry", "custom_time_tracking_source"):
            legacy_source = frappe.db.get_value(
                "Payroll Entry",
                payroll_entry,
                "custom_time_tracking_source",
            )
            if legacy_source == TIME_TRACKER_SOURCE:
                return TIME_TRACKER_SOURCE

    if stored_source == TIME_TRACKER_SOURCE:
        return TIME_TRACKER_SOURCE

    if cint(doc.get("salary_slip_based_on_timesheet")):
        return TIMESHEET_SOURCE

    return ""


def uses_time_tracker_salary_slip(doc) -> bool:
    return get_time_source(doc) == TIME_TRACKER_SOURCE


def prepare_time_tracker_hours(
    doc,
    *,
    lock: bool = False,
    recalculate: bool = False,
) -> list[frappe._dict]:
    """Apply weekly-capped Tracker Logs from the permanent Time Tracker.

    This mirrors the supplied Salary Slip server-script calculation while
    replacing standard Timesheet/Timesheet Detail rows with Time Tracker's own
    stopped Tracker Logs. The first and last Monday-Sunday buckets are
    prorated by calendar days. On submit, every selected Tracker Log is locked
    and allocated to the Salary Slip so an overlapping payroll cannot pay it
    twice.
    """

    source = get_time_source(doc)
    _set_if_available(doc, "custom_time_tracking_source", source)

    if source != TIME_TRACKER_SOURCE:
        return []

    # HRMS uses this internal flag to load an hourly Salary Structure and its
    # hourly earning component. The Payroll Entry's visible Timesheet checkbox
    # remains clear; Time Tracker performs the source-row selection itself.
    doc.salary_slip_based_on_timesheet = 1

    if not (doc.get("employee") and doc.get("start_date") and doc.get("end_date")):
        return []

    tracker = frappe.db.get_value(
        "Time Tracker",
        {"employee": doc.employee},
        "name",
    )

    if not tracker:
        frappe.throw(
            _("No Time Tracker exists for employee {0}.").format(
                frappe.bold(doc.employee)
            )
        )

    selected = _select_time_tracker_payroll_rows(doc, tracker, lock=lock)
    rows = selected.rows

    employee_settings = get_employee_time_tracker_pay_settings(
        doc.employee,
        currency=doc.get("currency"),
    )
    weekly_summary = build_weekly_time_tracker_summary(
        doc.start_date,
        doc.end_date,
        rows,
        employee_settings.weekly_limit,
    )

    tracked_hours = flt(weekly_summary.total_tracked_hours, 6)
    payable_hours = flt(weekly_summary.total_payable_hours, 6)
    exceeded_hours = flt(weekly_summary.total_exceeded_hours, 6)

    # Prevent standard Salary Slip submit/cancel logic from changing ERPNext
    # Timesheet status. Time Tracker reads Tracker Logs directly.
    doc.set("timesheets", [])

    # HRMS multiplies ``total_working_hours`` by ``hour_rate`` for the hourly
    # earning. Use capped payable hours here, not the raw tracked total.
    doc.total_working_hours = payable_hours

    _set_if_available(doc, "custom_time_tracker", tracker)
    # This legacy field was used briefly by 0.4.1. Clear it so a Time Tracker
    # Salary Slip never appears to depend on an ERPNext Timesheet.
    _set_if_available(doc, "custom_timesheet", None)
    _set_if_available(doc, "custom_payroll_hours_source", selected.source)
    _set_if_available(doc, "custom_time_tracker_hours", tracked_hours)
    _set_if_available(doc, "custom_time_tracker_log_count", len(rows))
    _set_if_available(doc, "custom_total_monthly_hours", payable_hours)
    _set_if_available(doc, "custom_total_exceeded_hours", exceeded_hours)
    _set_if_available(
        doc,
        "custom_weekly_hours_limit",
        employee_settings.weekly_limit,
    )

    _populate_weekly_summary_table(doc, weekly_summary.rows)

    # ``custom_hourly_rate_usd`` is the authoritative Employee value. A positive
    # Employee rate overrides the Salary Structure Hour Rate.
    _set_hourly_rate(
        doc,
        employee_hourly_rate=employee_settings.hourly_rate,
        strict=False,
    )

    if recalculate and doc.get("salary_structure"):
        salary_component = _get_hourly_salary_component(doc)
        _set_hourly_rate(
            doc,
            employee_hourly_rate=employee_settings.hourly_rate,
            strict=True,
        )
        wages_amount = flt(doc.hour_rate) * payable_hours
        doc.add_earning_for_hourly_wages(doc, salary_component, wages_amount)
        _synchronise_hourly_wage_component(doc, salary_component, wages_amount)
        _recalculate_salary_slip(doc)

    return rows


def pull_time_tracker_salary_structure(doc) -> None:
    """Load the hourly Salary Structure and apply weekly-capped source hours."""

    from hrms.payroll.doctype.salary_structure.salary_structure import make_salary_slip

    salary_structure_doc = getattr(doc, "_salary_structure_doc", None)
    if not salary_structure_doc:
        frappe.throw(_("Unable to load the hourly Salary Structure."))

    doc.salary_structure = salary_structure_doc.name

    employee_settings = get_employee_time_tracker_pay_settings(
        doc.employee,
        currency=doc.get("currency"),
    )
    _set_hourly_rate(
        doc,
        salary_structure_doc=salary_structure_doc,
        employee_hourly_rate=employee_settings.hourly_rate,
        strict=True,
    )

    prepare_time_tracker_hours(doc, recalculate=False)

    salary_component = salary_structure_doc.salary_component
    if not salary_component:
        frappe.throw(
            _(
                "Salary Structure {0} does not have an Hourly Salary Component."
            ).format(frappe.bold(salary_structure_doc.name))
        )

    wages_amount = apply_time_tracker_hourly_earning(
        doc,
        salary_component=salary_component,
    )
    make_salary_slip(salary_structure_doc.name, doc)
    _synchronise_hourly_wage_component(doc, salary_component, wages_amount)


def apply_time_tracker_hourly_earning(
    doc,
    *,
    salary_component: str,
) -> float:
    """Add hourly wages without allowing HRMS to reset capped Tracker hours.

    HRMS v15 calls ``pull_sal_struct`` for timesheet payroll. HRMS v16 calls
    ``add_timesheet_earning_component`` after ``set_time_sheet`` and normally
    replaces ``total_working_hours`` with the sum of ERPNext Timesheet rows.
    Time Tracker deliberately keeps that table empty, so both controller paths
    use this helper to retain the weekly-capped Tracker Log total.
    """

    if not salary_component:
        frappe.throw(
            _(
                "Salary Structure {0} does not have an Hourly Salary Component."
            ).format(frappe.bold(doc.get("salary_structure") or ""))
        )

    employee_settings = get_employee_time_tracker_pay_settings(
        doc.get("employee"),
        currency=doc.get("currency"),
    )
    _set_hourly_rate(
        doc,
        employee_hourly_rate=employee_settings.hourly_rate,
        strict=True,
    )

    wages_amount = flt(doc.hour_rate) * flt(doc.total_working_hours)
    doc.add_earning_for_hourly_wages(doc, salary_component, wages_amount)
    _synchronise_hourly_wage_component(doc, salary_component, wages_amount)
    return wages_amount


def check_duplicate_time_tracker_salary_slip(doc) -> None:
    """Reject any other live Salary Slip for the same employee and period."""

    duplicate = frappe.db.exists(
        "Salary Slip",
        {
            "employee": doc.employee,
            "start_date": doc.start_date,
            "end_date": doc.end_date,
            "docstatus": ["!=", 2],
            "name": ["!=", doc.name or ""],
        },
    )

    if duplicate:
        frappe.throw(
            _(
                "Salary Slip {0} already exists for employee {1} in this "
                "payroll period."
            ).format(frappe.bold(duplicate), frappe.bold(doc.employee)),
            title=_("Duplicate Salary Slip"),
        )


def allocate_time_tracker_logs(doc, method: str | None = None) -> None:
    """Allocate the exact Tracker Logs used by a draft/submitted Salary Slip."""

    del method

    if not uses_time_tracker_salary_slip(doc):
        return

    # Draft documents are always reselected under a row lock so recalculation
    # cannot reuse an allocation list left on the same in-memory document. A
    # submitted document may use the list captured by ``before_submit``.
    tracker_names = (
        getattr(doc.flags, _LOG_ALLOCATION_FLAG, None)
        if cint(doc.get("docstatus")) == 1
        else None
    )

    # Fallback for integrations that invoke ``on_submit`` without the standard
    # Frappe ``before_submit`` lifecycle.
    if tracker_names is None:
        tracker = doc.get("custom_time_tracker") or frappe.db.get_value(
            "Time Tracker",
            {"employee": doc.employee},
            "name",
        )
        if not tracker:
            frappe.throw(
                _("No Time Tracker exists for employee {0}.").format(
                    frappe.bold(doc.employee)
                )
            )

        _select_time_tracker_payroll_rows(doc, tracker, lock=True)
        tracker_names = getattr(doc.flags, _LOG_ALLOCATION_FLAG, [])

    tracker_names = list(dict.fromkeys(tracker_names or []))
    _release_unselected_tracker_logs(doc, tracker_names)
    _allocate_tracker_logs(doc, tracker_names)


def _release_unselected_tracker_logs(doc, log_names: list[str]) -> None:
    """Clear stale allocations when a draft Salary Slip is recalculated."""

    if not doc.name or not frappe.db.has_column("Tracker Log", "salary_slip"):
        return

    if not log_names:
        frappe.db.sql(
            """
            UPDATE `tabTracker Log`
            SET salary_slip = NULL
            WHERE salary_slip = %s
            """,
            (doc.name,),
        )
        return

    placeholders = ", ".join(["%s"] * len(log_names))
    frappe.db.sql(
        f"""
        UPDATE `tabTracker Log`
        SET salary_slip = NULL
        WHERE salary_slip = %s
          AND name NOT IN ({placeholders})
        """,
        (doc.name, *log_names),
    )


def _allocate_tracker_logs(doc, log_names: list[str]) -> None:
    if not log_names:
        return

    _require_tracker_log_allocation_field()
    placeholders = ", ".join(["%s"] * len(log_names))
    frappe.db.sql(
        f"""
        UPDATE `tabTracker Log`
        SET salary_slip = %s
        WHERE name IN ({placeholders})
          AND (salary_slip IS NULL OR salary_slip = '' OR salary_slip = %s)
        """,
        (doc.name, *log_names, doc.name),
    )

    allocated = set(
        frappe.get_all(
            "Tracker Log",
            filters={
                "name": ["in", log_names],
                "salary_slip": doc.name,
            },
            pluck="name",
            limit_page_length=0,
        )
    )
    missing = sorted(set(log_names) - allocated)

    if missing:
        frappe.throw(
            _(
                "Some Tracker Logs were assigned to another Salary Slip while "
                "this document was being saved. Reload and try again."
            ),
            title=_("Time Tracker Payroll Conflict"),
        )


def release_time_tracker_logs(doc, method: str | None = None) -> None:
    """Release Tracker Logs allocated to a cancelled Salary Slip.

    Version 0.4.1 briefly allowed Draft Timesheet rows as a payroll source.
    New payroll never reads those rows, but cancellation still clears the old
    allocation link so historical 0.4.1 slips do not leave rows permanently
    reserved after an upgrade.
    """

    del method

    if not doc.name:
        return

    if frappe.db.has_column("Tracker Log", "salary_slip"):
        frappe.db.sql(
            """
            UPDATE `tabTracker Log`
            SET salary_slip = NULL
            WHERE salary_slip = %s
            """,
            (doc.name,),
        )

    if frappe.db.has_column(
        "Timesheet Detail",
        LEGACY_TIMESHEET_DETAIL_ALLOCATION_FIELD,
    ):
        fieldname = LEGACY_TIMESHEET_DETAIL_ALLOCATION_FIELD
        frappe.db.sql(
            f"""
            UPDATE `tabTimesheet Detail`
            SET `{fieldname}` = NULL
            WHERE `{fieldname}` = %s
            """,
            (doc.name,),
        )


def reconcile_time_tracker_salary_slips_after_payroll_submit(
    doc,
    method: str | None = None,
) -> None:
    """Reconcile synchronous manual/automated Payroll Entry submissions.

    Salary Slip document events normally allocate each draft as it is inserted.
    This Payroll Entry event is an idempotent final pass for the synchronous
    path. Queued payrolls are completed by the Salary Slip events in the worker.
    """

    del method
    reconcile_time_tracker_salary_slips_for_payroll_entry(doc)


def reconcile_time_tracker_salary_slips_for_payroll_entry(
    payroll_entry,
    *,
    raise_on_error: bool = False,
) -> frappe._dict:
    """Repair Tracker Log allocations for every live slip in a Payroll Entry.

    New Salary Slips are linked by document events. This reconciliation is an
    idempotent second line of defence for queued payroll jobs, older 0.5.0
    drafts, and sites where another app also customises the Salary Slip class.
    It never submits a Salary Slip or changes its calculated salary values.
    """

    payroll_entry_name = (
        payroll_entry
        if isinstance(payroll_entry, str)
        else payroll_entry.get("name")
    )
    summary = frappe._dict(
        payroll_entry=payroll_entry_name,
        salary_slips=0,
        reconciled=0,
        skipped=0,
        linked_tracker_logs=0,
        linked_tracker_hours=0.0,
        errors=[],
    )

    if not payroll_entry_name:
        return summary
    if not frappe.db.has_column(
        "Payroll Entry",
        PAYROLL_ENTRY_TIME_TRACKER_FIELD,
    ):
        return summary
    if not cint(
        frappe.db.get_value(
            "Payroll Entry",
            payroll_entry_name,
            PAYROLL_ENTRY_TIME_TRACKER_FIELD,
        )
    ):
        return summary

    _require_tracker_log_allocation_field()

    salary_slips = frappe.get_all(
        "Salary Slip",
        filters={
            "payroll_entry": payroll_entry_name,
            "docstatus": ["!=", 2],
        },
        fields=["name", "docstatus"],
        order_by="start_date, employee, creation, name",
        limit_page_length=0,
    )
    summary.salary_slips = len(salary_slips)

    for index, row in enumerate(salary_slips, start=1):
        savepoint = f"time_tracker_relink_salary_slip_{index}"
        frappe.db.savepoint(savepoint)
        try:
            salary_slip = frappe.get_doc("Salary Slip", row.name)
            if not uses_time_tracker_salary_slip(salary_slip):
                summary.skipped += 1
                continue

            allocate_time_tracker_logs(salary_slip)
            linked_count, linked_hours = _get_linked_tracker_log_stats(
                salary_slip.name
            )

            expected_count = cint(
                salary_slip.get("custom_time_tracker_log_count")
            )
            if (
                salary_slip.meta.has_field("custom_time_tracker_log_count")
                and linked_count != expected_count
            ):
                frappe.throw(
                    _(
                        "Salary Slip {0} expects {1} Tracker Logs, but {2} "
                        "could be linked. Review overlapping payroll periods "
                        "or logs already allocated to another Salary Slip."
                    ).format(
                        frappe.bold(salary_slip.name),
                        expected_count,
                        linked_count,
                    ),
                    title=_("Tracker Log Link Count Mismatch"),
                )

            expected_hours = flt(
                salary_slip.get("custom_time_tracker_hours"),
                6,
            )
            if (
                salary_slip.meta.has_field("custom_time_tracker_hours")
                and abs(flt(linked_hours, 6) - expected_hours) > 0.00001
            ):
                frappe.throw(
                    _(
                        "Salary Slip {0} expects {1} tracked hours, but the "
                        "Tracker Logs that can be linked total {2} hours. "
                        "Review edited, deleted, newly-added, or overlapping "
                        "Tracker Logs before repairing this Salary Slip."
                    ).format(
                        frappe.bold(salary_slip.name),
                        expected_hours,
                        flt(linked_hours, 6),
                    ),
                    title=_("Tracker Log Hours Mismatch"),
                )

            summary.reconciled += 1
            summary.linked_tracker_logs += int(linked_count or 0)
            summary.linked_tracker_hours += flt(linked_hours, 6)
        except Exception as exc:
            frappe.db.rollback(save_point=savepoint)
            summary.errors.append(
                {
                    "salary_slip": row.name,
                    "message": str(exc),
                }
            )
            frappe.log_error(
                message=frappe.get_traceback(),
                title=f"Time Tracker log relink: {row.name}",
            )

            if raise_on_error:
                raise

    return summary


def _get_linked_tracker_log_stats(salary_slip: str) -> tuple[int, float]:
    """Return count and raw hours currently allocated to one Salary Slip."""

    values = frappe.db.sql(
        """
        SELECT COUNT(*), COALESCE(SUM(hours), 0)
        FROM `tabTracker Log`
        WHERE salary_slip = %s
        """,
        (salary_slip,),
    )[0]
    return int(values[0] or 0), flt(values[1], 6)


def release_time_tracker_logs_for_payroll_entry(
    doc,
    method: str | None = None,
) -> int:
    """Release every Tracker Log linked through a Payroll Entry's slips.

    HRMS deletes linked Salary Slips when a Payroll Entry is cancelled. The
    Salary Slip ``on_trash`` hook normally releases each allocation; this bulk
    release is an additional guard for interrupted jobs and legacy records.
    """

    del method

    payroll_entry = doc if isinstance(doc, str) else doc.get("name")
    if not payroll_entry or not frappe.db.has_column("Tracker Log", "salary_slip"):
        return 0

    salary_slips = frappe.get_all(
        "Salary Slip",
        filters={"payroll_entry": payroll_entry},
        pluck="name",
        limit_page_length=0,
    )
    if not salary_slips:
        return 0

    placeholders = ", ".join(["%s"] * len(salary_slips))
    linked_count = frappe.db.sql(
        f"""
        SELECT COUNT(*)
        FROM `tabTracker Log`
        WHERE salary_slip IN ({placeholders})
        """,
        tuple(salary_slips),
    )[0][0]
    frappe.db.sql(
        f"""
        UPDATE `tabTracker Log`
        SET salary_slip = NULL
        WHERE salary_slip IN ({placeholders})
        """,
        tuple(salary_slips),
    )

    if frappe.db.has_column(
        "Timesheet Detail",
        LEGACY_TIMESHEET_DETAIL_ALLOCATION_FIELD,
    ):
        fieldname = LEGACY_TIMESHEET_DETAIL_ALLOCATION_FIELD
        frappe.db.sql(
            f"""
            UPDATE `tabTimesheet Detail`
            SET `{fieldname}` = NULL
            WHERE `{fieldname}` IN ({placeholders})
            """,
            tuple(salary_slips),
        )

    return int(linked_count or 0)


def get_payable_tracker_logs(
    doc,
    tracker: str,
    *,
    lock: bool = False,
) -> list[frappe._dict]:
    """Return stopped in-period logs not allocated to another Salary Slip."""

    if not tracker:
        return []

    has_allocation_field = frappe.db.has_column("Tracker Log", "salary_slip")

    if lock and not has_allocation_field:
        _require_tracker_log_allocation_field()

    allocation_condition = ""
    params: list[Any] = [tracker, doc.employee, doc.start_date, doc.end_date]

    if has_allocation_field:
        allocation_condition = (
            "AND (salary_slip IS NULL OR salary_slip = '' OR salary_slip = %s)"
        )
        params.append(doc.name or "")

    lock_clause = "FOR UPDATE" if lock else ""

    return frappe.db.sql(
        f"""
        SELECT name, log_date, start_time, end_time, hours
        FROM `tabTracker Log`
        WHERE time_tracker = %s
          AND employee = %s
          AND status = 'Stopped'
          AND end_time IS NOT NULL
          AND COALESCE(hours, 0) > 0
          AND log_date BETWEEN %s AND %s
          {allocation_condition}
        ORDER BY log_date, start_time, name
        {lock_clause}
        """,
        tuple(params),
        as_dict=True,
    )


def _select_time_tracker_payroll_rows(
    doc,
    tracker: str,
    *,
    lock: bool = False,
) -> frappe._dict:
    """Select stopped, positive, unpaid Tracker Logs for the Salary Slip."""

    if not lock:
        # Never carry a previous draft allocation list into a later save or
        # submit performed with the same in-memory Document instance.
        doc.flags.pop(_LOG_ALLOCATION_FLAG, None)

    rows = get_payable_tracker_logs(doc, tracker, lock=lock)
    result = frappe._dict(
        source=TRACKER_LOG_SOURCE,
        rows=rows,
    )

    selected_names = [row.name for row in result.rows]
    selected_fingerprints = _hourly_source_row_fingerprints(result.rows)

    if not lock:
        setattr(
            doc.flags,
            _EXPECTED_HOURS_ROWS_FLAG,
            frappe._dict(
                source=result.source,
                fingerprints=selected_fingerprints,
            ),
        )

    if lock:
        expected = getattr(doc.flags, _EXPECTED_HOURS_ROWS_FLAG, None)
        if expected:
            expected_source = expected.get("source")
            expected_fingerprints = set(expected.get("fingerprints") or [])
            current_fingerprints = set(selected_fingerprints)

            if (
                expected_source != result.source
                or expected_fingerprints != current_fingerprints
            ):
                frappe.throw(
                    _(
                        "The Tracker Logs changed while this Salary Slip was "
                        "being submitted. A log may have been edited, deleted, "
                        "or allocated to another Salary Slip. Reload the "
                        "document, review the hours, and submit again."
                    ),
                    title=_("Time Tracker Payroll Source Changed"),
                )

        setattr(doc.flags, _LOG_ALLOCATION_FLAG, selected_names)

    return result


def _hourly_source_row_fingerprints(
    rows: list[frappe._dict],
) -> list[tuple[str, str, float]]:
    """Return the payroll-relevant identity of selected hourly source rows."""

    fingerprints: list[tuple[str, str, float]] = []
    for row in rows or []:
        log_date = getdate(row.get("log_date")) if row.get("log_date") else None
        fingerprints.append(
            (
                str(row.get("name") or ""),
                str(log_date or ""),
                flt(row.get("hours"), 6),
            )
        )

    return fingerprints


def get_employee_time_tracker_pay_settings(
    employee: str,
    *,
    currency: str | None = None,
) -> frappe._dict:
    """Return the Employee rate and weekly limit used by payroll.

    ``custom_hourly_rate_usd`` and ``custom_weekly_hours_limit`` are the
    preferred fields used by the site's Employee form and Time Tracker
    dashboard. ``custom_working_hours_weekly_limit`` remains supported as a
    synchronised compatibility alias for sites that installed version 0.4.8
    or earlier.

    The hourly-rate field is intentionally not suppressed when the Salary Slip
    currency is not literally ``USD``.  The site's business rule stores its
    payroll rate in that field and expects the numeric value to be applied
    directly, exactly like the supplied server script.
    """

    del currency  # Kept in the signature for compatibility with older callers.

    defaults = frappe._dict(
        hourly_rate=0.0,
        weekly_limit=DEFAULT_WEEKLY_HOURS_LIMIT,
        user_id=None,
        hourly_rate_field=None,
        weekly_limit_field=None,
        weekly_limit_conflict=False,
        weekly_limit_values={},
    )

    if not employee:
        return defaults

    employee_meta = frappe.get_meta("Employee")
    available_fields = [
        fieldname
        for fieldname in (
            *EMPLOYEE_HOURLY_RATE_FIELDS,
            *EMPLOYEE_WEEKLY_LIMIT_FIELDS,
            "user_id",
        )
        if employee_meta.has_field(fieldname)
    ]

    if not available_fields:
        return defaults

    values = frappe.db.get_value(
        "Employee",
        employee,
        available_fields,
        as_dict=True,
    ) or frappe._dict()

    return _resolve_employee_time_tracker_pay_settings(
        values,
        available_fields=available_fields,
    )


def _resolve_employee_time_tracker_pay_settings(
    values,
    *,
    available_fields: list[str] | tuple[str, ...] | set[str] | None = None,
) -> frappe._dict:
    """Resolve Employee payroll values without any database dependency.

    Keeping this small resolver pure makes field-priority and default behavior
    regression-testable outside a live Bench.
    """

    values = frappe._dict(values or {})
    available = set(available_fields or values.keys())
    result = frappe._dict(
        hourly_rate=0.0,
        weekly_limit=DEFAULT_WEEKLY_HOURS_LIMIT,
        user_id=values.get("user_id"),
        hourly_rate_field=None,
        weekly_limit_field=None,
        weekly_limit_conflict=False,
        weekly_limit_values={},
    )

    for fieldname in EMPLOYEE_HOURLY_RATE_FIELDS:
        if fieldname not in available:
            continue

        value = flt(values.get(fieldname))
        if value > 0:
            result.hourly_rate = value
            result.hourly_rate_field = fieldname
            break

    weekly_settings = resolve_employee_weekly_hours_limit(
        values,
        available_fields=available,
    )
    result.weekly_limit = weekly_settings.weekly_limit
    result.weekly_limit_field = weekly_settings.weekly_limit_field
    result.weekly_limit_conflict = weekly_settings.weekly_limit_conflict
    result.weekly_limit_values = weekly_settings.weekly_limit_values

    return result


def resolve_employee_weekly_hours_limit(
    values,
    *,
    available_fields: list[str] | tuple[str, ...] | set[str] | None = None,
) -> frappe._dict:
    """Resolve duplicate Employee weekly-limit fields deterministically.

    Version 0.4.8 could leave both supported fields on an Employee. The
    app-created alias defaulted to 40, while the site's original field could
    contain an explicitly edited value such as 10. A simple first-field lookup
    therefore made payroll use 40 even though the dashboard showed 10.

    Resolution rules are intentionally migration-safe:

    * a single positive value wins;
    * when one value is the default 40 and the other is non-default, the
      non-default value wins (it is the likely explicit configuration);
    * when both positive, non-default values conflict, the visible canonical
      ``custom_weekly_hours_limit`` field wins;
    * non-positive or missing values fall back to 40.
    """

    # ``values`` is normally a mapping when called from payroll, but the
    # Time Tracker dashboard and Employee hooks pass a Frappe Document. A
    # Document exposes ``get`` but is not iterable, so passing it directly to
    # ``frappe._dict`` raises ``TypeError: object is not iterable``.
    if values is not None and not isinstance(values, dict):
        get_value = getattr(values, "get", None)
        if callable(get_value):
            fieldnames = available_fields or EMPLOYEE_WEEKLY_LIMIT_FIELDS
            values = {
                fieldname: get_value(fieldname)
                for fieldname in fieldnames
            }

    values = frappe._dict(values or {})
    available = set(available_fields or values.keys())
    candidates: list[tuple[str, float]] = []

    for fieldname in EMPLOYEE_WEEKLY_LIMIT_FIELDS:
        if fieldname not in available:
            continue

        value = flt(values.get(fieldname))
        if value > 0:
            candidates.append((fieldname, value))

    if not candidates:
        return frappe._dict(
            weekly_limit=DEFAULT_WEEKLY_HOURS_LIMIT,
            weekly_limit_field=None,
            weekly_limit_conflict=False,
            weekly_limit_values={},
        )

    values_by_field = {
        fieldname: flt(value, 6) for fieldname, value in candidates
    }
    distinct_values = {
        round(flt(value), 6) for _, value in candidates
    }
    conflict = len(distinct_values) > 1

    chosen_field, chosen_value = candidates[0]
    non_default_candidates = [
        (fieldname, value)
        for fieldname, value in candidates
        if abs(flt(value) - DEFAULT_WEEKLY_HOURS_LIMIT) > 0.000001
    ]

    if len(non_default_candidates) == 1 and len(candidates) > 1:
        # The other field is normally the app-created untouched default.
        chosen_field, chosen_value = non_default_candidates[0]

    return frappe._dict(
        weekly_limit=flt(chosen_value),
        weekly_limit_field=chosen_field,
        weekly_limit_conflict=conflict,
        weekly_limit_values=values_by_field,
    )


def build_weekly_time_tracker_summary(
    start_date,
    end_date,
    logs: list[frappe._dict],
    weekly_limit: float,
) -> frappe._dict:
    """Build Monday-Sunday buckets and proportional weekly caps.

    The first bucket begins on ``start_date`` and closes on the first Sunday.
    Every middle bucket is Monday-Sunday.  The final bucket ends on
    ``end_date``.  ``days_in_period`` includes calendar days, exactly like the
    supplied business logic.
    """

    period_start = getdate(start_date)
    period_end = getdate(end_date)

    if period_start > period_end:
        frappe.throw(_("Salary Slip Start Date cannot be after End Date."))

    weekly_limit = flt(weekly_limit)
    if weekly_limit <= 0:
        weekly_limit = DEFAULT_WEEKLY_HOURS_LIMIT

    buckets: list[frappe._dict] = []
    date_to_bucket: dict[str, int] = {}
    current_date = period_start
    week_number = 1
    current_bucket: frappe._dict | None = None

    while current_date <= period_end:
        if current_bucket is None:
            current_bucket = frappe._dict(
                week=_("Week {0}").format(week_number),
                period_start=current_date,
                period_end=current_date,
                days_in_period=0,
                tracked_hours=0.0,
            )
            buckets.append(current_bucket)

        current_bucket.days_in_period = cint(current_bucket.days_in_period) + 1
        current_bucket.period_end = current_date
        date_to_bucket[str(current_date)] = len(buckets) - 1

        # Python weekday: Monday=0, Sunday=6.
        if current_date.weekday() == 6 and current_date < period_end:
            week_number += 1
            current_bucket = None

        current_date = add_days(current_date, 1)

    for log in logs or []:
        log_date = getdate(log.get("log_date")) if log.get("log_date") else None
        if not log_date:
            continue

        bucket_index = date_to_bucket.get(str(log_date))
        if bucket_index is None:
            continue

        bucket = buckets[bucket_index]
        bucket.tracked_hours = flt(bucket.tracked_hours) + flt(log.get("hours"))

    total_tracked_hours = 0.0
    total_payable_hours = 0.0
    total_exceeded_hours = 0.0
    output_rows: list[frappe._dict] = []

    for bucket in buckets:
        days_in_period = cint(bucket.days_in_period)
        tracked_hours = flt(bucket.tracked_hours, 3)
        period_cap = flt(
            weekly_limit * (days_in_period / 7.0),
            3,
        )

        payable_hours = tracked_hours
        exceeded_hours = 0.0

        if tracked_hours > period_cap:
            payable_hours = period_cap
            exceeded_hours = flt(tracked_hours - period_cap, 3)

        output_rows.append(
            frappe._dict(
                week=bucket.week,
                period_start=bucket.period_start,
                period_end=bucket.period_end,
                days_in_period=days_in_period,
                weekly_limit=flt(weekly_limit, 3),
                period_cap=period_cap,
                tracked_hours=tracked_hours,
                payable_hours=payable_hours,
                exceeded_hours=exceeded_hours,
            )
        )

        total_tracked_hours += tracked_hours
        total_payable_hours += payable_hours
        total_exceeded_hours += exceeded_hours

    return frappe._dict(
        rows=output_rows,
        total_tracked_hours=flt(total_tracked_hours, 3),
        total_payable_hours=flt(total_payable_hours, 3),
        total_exceeded_hours=flt(total_exceeded_hours, 3),
        weekly_limit=flt(weekly_limit, 3),
    )


def _populate_weekly_summary_table(
    doc,
    rows: list[frappe._dict],
) -> None:
    """Store weekly rows as JSON so no child DocType is required."""

    if not doc.meta.has_field(SALARY_SLIP_WEEKLY_SUMMARY_JSON_FIELD):
        return

    serialisable_rows = [
        {
            "week": row.week,
            "period_start": str(row.period_start),
            "period_end": str(row.period_end),
            "days_in_period": row.days_in_period,
            "weekly_limit": row.weekly_limit,
            "period_cap": row.period_cap,
            "tracked_hours": row.tracked_hours,
            "payable_hours": row.payable_hours,
            "exceeded_hours": row.exceeded_hours,
        }
        for row in rows
    ]
    doc.set(
        SALARY_SLIP_WEEKLY_SUMMARY_JSON_FIELD,
        frappe.as_json(serialisable_rows),
    )

def _get_hourly_salary_component(doc) -> str:
    salary_structure_doc = getattr(doc, "_salary_structure_doc", None)
    salary_component = (
        salary_structure_doc.salary_component
        if salary_structure_doc
        else frappe.db.get_value(
            "Salary Structure",
            doc.salary_structure,
            "salary_component",
        )
    )

    if not salary_component:
        frappe.throw(
            _(
                "Salary Structure {0} does not have an Hourly Salary Component."
            ).format(frappe.bold(doc.salary_structure))
        )

    return salary_component


def _set_hourly_rate(
    doc,
    *,
    salary_structure_doc=None,
    employee_hourly_rate: float = 0.0,
    strict: bool,
) -> float:
    """Set the rate used by HRMS and expose it in the custom Salary Slip field."""

    rate = flt(employee_hourly_rate)

    if rate <= 0 and salary_structure_doc:
        rate = flt(salary_structure_doc.get("hour_rate"))

    if rate <= 0 and flt(doc.get("hour_rate")) > 0:
        rate = flt(doc.get("hour_rate"))

    if rate <= 0 and doc.get("salary_structure"):
        rate = flt(
            frappe.db.get_value(
                "Salary Structure",
                doc.salary_structure,
                "hour_rate",
            )
        )

    if strict and rate <= 0:
        frappe.throw(
            _(
                "Set a positive custom_hourly_rate_usd on Employee {0}, or "
                "set a positive Hour Rate on Salary Structure {1}."
            ).format(
                frappe.bold(doc.get("employee") or ""),
                frappe.bold(doc.get("salary_structure") or ""),
            ),
            title=_("Hourly Rate Required"),
        )

    if rate > 0:
        doc.hour_rate = rate
        doc.base_hour_rate = flt(rate) * flt(doc.get("exchange_rate") or 1)
        _set_if_available(doc, "custom_hourly_rate", rate)

    return rate


def _recalculate_salary_slip(doc) -> None:
    doc.calculate_net_pay()
    doc.compute_year_to_date()
    doc.compute_month_to_date()
    doc.compute_component_wise_year_to_date()


def _require_tracker_log_allocation_field() -> None:
    if frappe.db.has_column("Tracker Log", "salary_slip"):
        return

    frappe.throw(
        _(
            "Tracker Log payroll allocation is not installed. Run bench migrate "
            "for the Time Tracker app before submitting Salary Slips."
        )
    )


def _synchronise_hourly_wage_component(
    doc,
    salary_component: str,
    amount: float,
) -> None:
    """Keep current and default hourly amounts aligned for payroll/tax logic."""

    for row in doc.get("earnings") or []:
        if row.salary_component != salary_component or row.get("additional_salary"):
            continue

        row.amount = amount
        row.default_amount = amount
        row.additional_amount = 0
        return


def _set_if_available(doc, fieldname: str, value) -> None:
    if doc.meta.has_field(fieldname):
        doc.set(fieldname, value)


# Compatibility aliases for sites upgrading from the previous patch.
def apply_time_tracker_hours(doc, method: str | None = None) -> None:
    prepare_time_tracker_hours(
        doc,
        lock=method == "before_submit",
        recalculate=True,
    )


def _get_time_source(doc) -> str:
    return get_time_source(doc)


def _uses_time_tracker_payroll_entry(doc) -> bool:
    return uses_time_tracker_payroll_entry(doc)
