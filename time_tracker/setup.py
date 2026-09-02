from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_field
from frappe.utils import cint, flt

from time_tracker.events.employee import sync_all_time_tracker_statuses
from time_tracker.payroll import (
    EMPLOYEE_WEEKLY_LIMIT_FIELDS,
    PAYROLL_ENTRY_AUTOMATION_MODE_FIELD,
    SALARY_SLIP_WEEKLY_SUMMARY_JSON_FIELD,
    SALARY_STRUCTURE_TIME_TRACKER_FIELD,
    reconcile_time_tracker_salary_slips_for_payroll_entry,
    resolve_employee_weekly_hours_limit,
)
from time_tracker.permissions import ROLE_LOG_EDITOR, ROLE_MANAGER
from time_tracker.print_formats import (
    LEGACY_SALARY_SLIP_PRINT_FORMAT,
    SALARY_SLIP_PRINT_FORMAT,
    ensure_salary_slip_print_format,
)


ROLE_USER = "Time Tracker User"
LEGACY_PAYROLL_SOURCE_FIELD = "Payroll Entry-custom_time_tracking_source"
TRACKER_MODULE = "time_tracker"
CANONICAL_WEEKLY_LIMIT_FIELD = "custom_weekly_hours_limit"
LEGACY_WEEKLY_LIMIT_FIELD = "custom_working_hours_weekly_limit"
CORRECTION_REQUEST_DOCTYPE = "Time Tracker Correction Request"
CORRECTION_WORKFLOW = "Time Tracker Correction Request Workflow"
LEGACY_DOCTYPES = (
    "Time Tracker Settings",
    "Time Tracker Company Automation",
    "Time Tracker Weekly Summary",
)


def before_install() -> None:
    ensure_roles()


def before_migrate() -> None:
    ensure_roles()


def after_install() -> None:
    ensure_roles()
    ensure_core_doctype_schema()
    ensure_custom_fields()
    ensure_salary_slip_print_format()
    ensure_correction_request_workflow()
    ensure_default_company_settings()
    repair_tracker_log_ticket_doctype()
    sync_all_time_tracker_statuses()


def after_migrate() -> None:
    ensure_roles()
    ensure_core_doctype_schema()
    ensure_custom_fields()
    ensure_salary_slip_print_format()
    ensure_correction_request_workflow()
    migrate_legacy_time_tracker_settings()
    ensure_default_company_settings()
    migrate_legacy_salary_structure_mode()
    migrate_legacy_weekly_summary()
    cleanup_legacy_metadata()
    repair_tracker_log_ticket_doctype()
    sync_all_time_tracker_statuses()
    repair_tracker_log_payroll_fields()
    repair_draft_time_tracker_salary_slips()
    repair_time_tracker_salary_slip_links()


def ensure_core_doctype_schema() -> None:
    """Reload and verify the four DocTypes owned by Time Tracker."""

    required_columns = {
        "Time Tracker": ("time_tracker", "enable_browser_widget"),
        "Tracker Log": ("tracker_log", "ticket_doctype"),
        CORRECTION_REQUEST_DOCTYPE: (
            "time_tracker_correction_request",
            "workflow_state",
        ),
        "Time Tracker Setting": ("time_tracker_setting", "company"),
    }

    for doctype, (document_name, required_column) in required_columns.items():
        ready = bool(
            frappe.db.exists("DocType", doctype)
            and frappe.db.table_exists(doctype)
            and frappe.db.has_column(doctype, required_column)
            and frappe.get_meta(doctype, cached=False).has_field(required_column)
        )
        if ready:
            continue

        frappe.reload_doc(TRACKER_MODULE, "doctype", document_name, force=True)
        frappe.clear_cache(doctype=doctype)
        if not (
            frappe.db.exists("DocType", doctype)
            and frappe.db.table_exists(doctype)
            and frappe.db.has_column(doctype, required_column)
            and frappe.get_meta(doctype, cached=False).has_field(required_column)
        ):
            frappe.throw(
                _("Time Tracker migration could not install {0}.{1}.").format(
                    frappe.bold(doctype), frappe.bold(required_column)
                )
            )


def ensure_roles() -> None:
    for role_name in (ROLE_USER, ROLE_MANAGER, ROLE_LOG_EDITOR):
        if frappe.db.exists("Role", role_name):
            continue
        frappe.get_doc(
            {"doctype": "Role", "role_name": role_name, "desk_access": 1}
        ).insert(ignore_permissions=True)


def ensure_custom_fields() -> None:
    """Create and update Time Tracker's fields on standard HRMS DocTypes."""

    custom_fields: dict[str, list[dict[str, object]]] = {}

    if frappe.db.exists("DocType", "Employee"):
        weekly_limit_definition: dict[str, object] = {
            "fieldname": CANONICAL_WEEKLY_LIMIT_FIELD,
            "label": "Weekly Hours Limit",
            "fieldtype": "Float",
            "precision": 3,
            "hidden": 0,
            "read_only": 0,
            "default": "40",
            "insert_after": "status",
            "description": (
                "Maximum payable hours for a full Monday-Sunday week. "
                "Partial weeks are prorated by calendar days."
            ),
        }
        hourly_rate_definition: dict[str, object] = {
            "fieldname": "custom_hourly_rate_usd",
            "label": "Hourly Rate USD",
            "fieldtype": "Currency",
            "default": "0",
            "insert_after": CANONICAL_WEEKLY_LIMIT_FIELD,
            "description": (
                "Authoritative hourly payroll rate used by Time Tracker. A "
                "positive value overrides the Hour Rate on the assigned "
                "Time Tracker Salary Structure."
            ),
        }
        employee_fields = [weekly_limit_definition, hourly_rate_definition]
        if _doctype_has_field("Employee", LEGACY_WEEKLY_LIMIT_FIELD):
            employee_fields.append(
                {
                    "fieldname": LEGACY_WEEKLY_LIMIT_FIELD,
                    "label": "Legacy Weekly Hours Limit (Synced)",
                    "fieldtype": "Float",
                    "precision": 3,
                    "hidden": 1,
                    "read_only": 1,
                    "no_copy": 1,
                    "description": "Compatibility alias synchronised with Weekly Hours Limit.",
                }
            )
        custom_fields["Employee"] = employee_fields

    if frappe.db.exists("DocType", "Salary Structure"):
        custom_fields["Salary Structure"] = [
            {
                "fieldname": SALARY_STRUCTURE_TIME_TRACKER_FIELD,
                "label": "Based on Time Tracker",
                "fieldtype": "Check",
                "insert_after": "salary_slip_based_on_timesheet",
                "default": "0",
                "description": (
                    "Use Tracker Logs for hourly payroll. This is independent "
                    "from ERPNext's Salary Slip Based on Timesheet option."
                ),
            }
        ]

    if frappe.db.exists("DocType", "Payroll Entry"):
        custom_fields["Payroll Entry"] = [
            {
                "fieldname": "custom_pay_using_time_tracker",
                "label": "Pay Using Time Tracker",
                "fieldtype": "Check",
                "insert_after": "salary_slip_based_on_timesheet",
                "default": "0",
                "description": (
                    "Use stopped, positive, unpaid Tracker Logs. This option "
                    "is separate from standard Timesheet payroll."
                ),
            },
            {
                "fieldname": PAYROLL_ENTRY_AUTOMATION_MODE_FIELD,
                "label": "Time Tracker Automation Mode",
                "fieldtype": "Select",
                "insert_after": "custom_pay_using_time_tracker",
                "options": "\nTime Tracker\nFixed Salary",
                "hidden": 1,
                "read_only": 1,
                "allow_on_submit": 1,
                "no_copy": 1,
                "description": "Internal idempotency marker for monthly payroll automation.",
            },
        ]

    if frappe.db.exists("DocType", "Salary Slip"):
        custom_fields["Salary Slip"] = [
            {
                "fieldname": "custom_time_tracker_section",
                "label": "Time Tracker Payroll",
                "fieldtype": "Section Break",
                "insert_after": "timesheets",
                "depends_on": "eval:doc.custom_time_tracking_source == 'Time Tracker'",
            },
            {
                "fieldname": "custom_time_tracking_source",
                "label": "Payroll Mode",
                "fieldtype": "Select",
                "insert_after": "custom_time_tracker_section",
                "options": "\nTimesheet\nTime Tracker",
                "read_only": 1,
            },
            {
                "fieldname": "custom_payroll_hours_source",
                "label": "Hours Source",
                "fieldtype": "Select",
                "insert_after": "custom_time_tracking_source",
                "options": "\nTracker Log\nDraft Timesheet",
                "read_only": 1,
                "description": "Time Tracker payroll reads stopped Tracker Logs directly.",
            },
            {
                "fieldname": "custom_timesheet",
                "label": "Legacy Timesheet Reference",
                "fieldtype": "Link",
                "options": "Timesheet",
                "insert_after": "custom_payroll_hours_source",
                "read_only": 1,
                "hidden": 1,
            },
            {
                "fieldname": "custom_time_tracker",
                "label": "Permanent Time Tracker",
                "fieldtype": "Link",
                "options": "Time Tracker",
                "insert_after": "custom_payroll_hours_source",
                "read_only": 1,
            },
            {
                "fieldname": "custom_time_tracker_hours",
                "label": "Tracked Hours",
                "fieldtype": "Float",
                "insert_after": "custom_time_tracker",
                "precision": 6,
                "read_only": 1,
            },
            {
                "fieldname": "custom_time_tracker_log_count",
                "label": "Tracker Log Rows",
                "fieldtype": "Int",
                "insert_after": "custom_time_tracker_hours",
                "read_only": 1,
            },
            {
                "fieldname": "custom_hourly_rate",
                "label": "Applied Hourly Rate",
                "fieldtype": "Currency",
                "options": "currency",
                "insert_after": "custom_time_tracker_log_count",
                "read_only": 1,
            },
            {
                "fieldname": "custom_total_monthly_hours",
                "label": "Total Payable Hours",
                "fieldtype": "Float",
                "insert_after": "custom_hourly_rate",
                "precision": 3,
                "read_only": 1,
            },
            {
                "fieldname": "custom_total_exceeded_hours",
                "label": "Total Exceeded Hours",
                "fieldtype": "Float",
                "insert_after": "custom_total_monthly_hours",
                "precision": 3,
                "read_only": 1,
            },
            {
                "fieldname": SALARY_SLIP_WEEKLY_SUMMARY_JSON_FIELD,
                "label": "Weekly Time Tracker Summary (JSON)",
                "fieldtype": "Code",
                "options": "JSON",
                "insert_after": "custom_total_exceeded_hours",
                "hidden": 1,
                "read_only": 1,
                "allow_on_submit": 1,
                "no_copy": 1,
                "description": "Weekly payroll summary stored without a child DocType.",
            },
        ]

    for doctype, field_definitions in custom_fields.items():
        for field_definition in field_definitions:
            _create_custom_field_if_missing(doctype, field_definition)

    _synchronise_managed_custom_field_properties(custom_fields)
    _migrate_legacy_payroll_fields()
    reconcile_employee_weekly_limit_aliases()

    for doctype in custom_fields:
        frappe.clear_cache(doctype=doctype)
    frappe.db.commit()


def ensure_correction_request_workflow() -> None:
    if not frappe.db.exists("DocType", CORRECTION_REQUEST_DOCTYPE):
        return

    for state_name, style in (
        ("Requested", "Primary"),
        ("Approved", "Success"),
        ("Rejected", "Danger"),
        ("Updated", "Success"),
    ):
        if not frappe.db.exists("Workflow State", state_name):
            frappe.get_doc(
                {
                    "doctype": "Workflow State",
                    "workflow_state_name": state_name,
                    "style": style,
                }
            ).insert(ignore_permissions=True)

    for action_name in ("Approve", "Reject", "Update Tracker Log"):
        if not frappe.db.exists("Workflow Action Master", action_name):
            frappe.get_doc(
                {
                    "doctype": "Workflow Action Master",
                    "workflow_action_name": action_name,
                }
            ).insert(ignore_permissions=True)

    values = {
        "workflow_name": CORRECTION_WORKFLOW,
        "document_type": CORRECTION_REQUEST_DOCTYPE,
        "is_active": 1,
        "send_email_alert": 0,
        "workflow_state_field": "workflow_state",
        "states": [
            {"state": "Requested", "doc_status": 0, "allow_edit": ROLE_USER},
            {"state": "Approved", "doc_status": 0, "allow_edit": ROLE_MANAGER},
            {"state": "Rejected", "doc_status": 0, "allow_edit": ROLE_MANAGER},
            {"state": "Updated", "doc_status": 0, "allow_edit": ROLE_MANAGER},
        ],
        "transitions": [
            {
                "state": "Requested",
                "action": "Approve",
                "next_state": "Approved",
                "allowed": ROLE_MANAGER,
                "allow_self_approval": 0,
            },
            {
                "state": "Requested",
                "action": "Reject",
                "next_state": "Rejected",
                "allowed": ROLE_MANAGER,
                "allow_self_approval": 0,
            },
            {
                "state": "Approved",
                "action": "Update Tracker Log",
                "next_state": "Updated",
                "allowed": ROLE_MANAGER,
                "allow_self_approval": 0,
            },
        ],
    }

    existing_name = frappe.db.get_value(
        "Workflow", {"workflow_name": CORRECTION_WORKFLOW}, "name"
    )
    if existing_name:
        workflow = frappe.get_doc("Workflow", existing_name)
        workflow.update(values)
        workflow.set("states", values["states"])
        workflow.set("transitions", values["transitions"])
        workflow.save(ignore_permissions=True)
    else:
        frappe.get_doc({"doctype": "Workflow", **values}).insert(
            ignore_permissions=True
        )

    for other in frappe.get_all(
        "Workflow",
        filters={
            "document_type": CORRECTION_REQUEST_DOCTYPE,
            "is_active": 1,
            "workflow_name": ["!=", CORRECTION_WORKFLOW],
        },
        pluck="name",
        limit_page_length=0,
    ):
        frappe.db.set_value("Workflow", other, "is_active", 0)


def migrate_legacy_time_tracker_settings() -> int:
    """Move legacy singleton/child-table values without importing old controllers."""

    if not (
        frappe.db.exists("DocType", "Time Tracker Setting")
        and frappe.db.exists("DocType", "Time Tracker Settings")
        and frappe.db.table_exists("Time Tracker Company Automation")
    ):
        return 0

    singleton_rows = frappe.db.sql(
        """
        SELECT field, value
        FROM `tabSingles`
        WHERE doctype = %s
        """,
        "Time Tracker Settings",
        as_dict=True,
    )
    legacy = frappe._dict(
        {row.field: row.value for row in singleton_rows if row.field}
    )
    company_rows = frappe.db.sql(
        """
        SELECT *
        FROM `tabTime Tracker Company Automation`
        WHERE parent = %s
        ORDER BY idx
        """,
        "Time Tracker Settings",
        as_dict=True,
    )

    created = 0
    for raw_row in company_rows:
        row = frappe._dict(raw_row)
        if not row.company or frappe.db.exists("Time Tracker Setting", row.company):
            continue

        monthly_enabled = bool(
            cint(legacy.get("enable_monthly_time_tracker_payroll"))
            and cint(row.get("enable_monthly_time_tracker_payroll"))
        )
        monthly_ready = bool(
            row.get("currency")
            and row.get("payroll_payable_account")
            and row.get("cost_center")
            and flt(row.get("exchange_rate")) > 0
        )

        values = {
            "doctype": "Time Tracker Setting",
            "enabled": cint(row.get("enabled", 1)),
            "company": row.company,
            "currency": row.get("currency"),
            "show_salary_slip_on_time_tracker": 1,
            "show_pay_using_time_tracker_in_payroll_entry": 1,
            "salary_slip_print_format": SALARY_SLIP_PRINT_FORMAT,
            "enable_employee_automation": cint(
                legacy.get("enable_employee_automation", 1)
            ),
            "auto_assign_salary_structure": cint(
                legacy.get("auto_assign_salary_structure", 0)
            ),
            "auto_create_time_tracker": cint(
                legacy.get("auto_create_time_tracker", 1)
            ),
            "auto_create_appraisal": cint(
                legacy.get("auto_create_appraisal", 0)
            ),
            "auto_allocate_leave": cint(
                legacy.get("auto_allocate_leave", 0)
            ),
            "default_salary_structure": row.get("default_salary_structure"),
            "default_assignment_base": row.get("default_assignment_base"),
            "default_assignment_variable": row.get("default_assignment_variable"),
            "income_tax_slab": row.get("income_tax_slab"),
            "payroll_payable_account": row.get("payroll_payable_account"),
            "appraisal_cycle": row.get("appraisal_cycle"),
            "appraisal_template": row.get("appraisal_template"),
            "leave_policy": row.get("leave_policy"),
            "leave_assignment_based_on": (
                row.get("leave_assignment_based_on") or "Joining Date"
            ),
            "leave_period": row.get("leave_period"),
            "carry_forward": cint(row.get("carry_forward")),
            "enable_auto_create_monthly_payroll": int(
                monthly_enabled and monthly_ready
            ),
            "cost_center": row.get("cost_center"),
            "exchange_rate": flt(row.get("exchange_rate")) or 1,
        }
        frappe.get_doc(values).insert(ignore_permissions=True)
        created += 1

    return created


def ensure_default_company_settings() -> int:
    if not frappe.db.exists("DocType", "Time Tracker Setting"):
        return 0

    created = 0
    for company in frappe.get_all("Company", pluck="name", order_by="name"):
        if frappe.db.exists("Time Tracker Setting", company):
            continue
        frappe.get_doc(
            {
                "doctype": "Time Tracker Setting",
                "company": company,
                "enabled": 1,
                "show_salary_slip_on_time_tracker": 1,
                "show_pay_using_time_tracker_in_payroll_entry": 1,
                "salary_slip_print_format": SALARY_SLIP_PRINT_FORMAT,
                "enable_employee_automation": 1,
                "auto_create_time_tracker": 1,
                "auto_assign_salary_structure": 0,
                "auto_create_appraisal": 0,
                "auto_allocate_leave": 0,
                "enable_auto_create_monthly_payroll": 0,
                "exchange_rate": 1,
            }
        ).insert(ignore_permissions=True)
        created += 1
    return created


def migrate_legacy_salary_structure_mode() -> int:
    if not (
        frappe.db.has_column("Salary Structure", SALARY_STRUCTURE_TIME_TRACKER_FIELD)
        and frappe.db.has_column("Salary Structure", "salary_slip_based_on_timesheet")
    ):
        return 0

    structures: set[str] = set()
    if frappe.db.exists("DocType", "Time Tracker"):
        rows = frappe.db.sql(
            """
            SELECT DISTINCT ssa.salary_structure
            FROM `tabSalary Structure Assignment` ssa
            INNER JOIN `tabTime Tracker` tt ON tt.employee = ssa.employee
            INNER JOIN `tabSalary Structure` ss ON ss.name = ssa.salary_structure
            WHERE ssa.docstatus = 1
              AND IFNULL(ss.salary_slip_based_on_timesheet, 0) = 1
              AND IFNULL(ss.custom_based_on_time_tracker, 0) = 0
            """,
            as_dict=True,
        )
        structures.update(row.salary_structure for row in rows)

    if frappe.db.has_column("Salary Slip", "custom_time_tracking_source"):
        structures.update(
            frappe.get_all(
                "Salary Slip",
                filters={"custom_time_tracking_source": "Time Tracker"},
                pluck="salary_structure",
                limit_page_length=0,
            )
        )

    structures.discard(None)
    if not structures:
        return 0

    frappe.db.sql(
        """
        UPDATE `tabSalary Structure`
        SET custom_based_on_time_tracker = 1,
            salary_slip_based_on_timesheet = 0
        WHERE name IN %(structures)s
        """,
        {"structures": tuple(sorted(structures))},
    )
    return len(structures)


def migrate_legacy_weekly_summary() -> int:
    if not (
        frappe.db.has_column("Salary Slip", SALARY_SLIP_WEEKLY_SUMMARY_JSON_FIELD)
        and frappe.db.table_exists("Time Tracker Weekly Summary")
    ):
        return 0

    rows = frappe.db.sql(
        """
        SELECT parent, week, period_start, period_end, days_in_period,
               weekly_limit, period_cap, tracked_hours, payable_hours,
               exceeded_hours
        FROM `tabTime Tracker Weekly Summary`
        ORDER BY parent, idx
        """,
        as_dict=True,
    )
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row.parent].append(
            {
                "week": row.week,
                "period_start": str(row.period_start) if row.period_start else None,
                "period_end": str(row.period_end) if row.period_end else None,
                "days_in_period": row.days_in_period,
                "weekly_limit": row.weekly_limit,
                "period_cap": row.period_cap,
                "tracked_hours": row.tracked_hours,
                "payable_hours": row.payable_hours,
                "exceeded_hours": row.exceeded_hours,
            }
        )

    updated = 0
    for salary_slip, summary_rows in grouped.items():
        if not frappe.db.exists("Salary Slip", salary_slip):
            continue
        current = frappe.db.get_value(
            "Salary Slip", salary_slip, SALARY_SLIP_WEEKLY_SUMMARY_JSON_FIELD
        )
        if current:
            continue
        frappe.db.set_value(
            "Salary Slip",
            salary_slip,
            SALARY_SLIP_WEEKLY_SUMMARY_JSON_FIELD,
            frappe.as_json(summary_rows),
            update_modified=False,
        )
        updated += 1
    return updated


def cleanup_legacy_metadata() -> None:
    legacy_custom_fields = (
        LEGACY_PAYROLL_SOURCE_FIELD,
        "Salary Slip-custom_weekly_timesheet_summary",
        "Salary Slip-custom_weekly_time_tracker_summary",
    )
    managed_custom_fields = {
        f"Payroll Entry-{PAYROLL_ENTRY_AUTOMATION_MODE_FIELD}",
        "Payroll Entry-custom_pay_using_time_tracker",
        f"Salary Structure-{SALARY_STRUCTURE_TIME_TRACKER_FIELD}",
        "Employee-custom_hourly_rate_usd",
        f"Employee-{CANONICAL_WEEKLY_LIMIT_FIELD}",
        "Salary Slip-custom_time_tracker_section",
        "Salary Slip-custom_time_tracking_source",
        "Salary Slip-custom_payroll_hours_source",
        "Salary Slip-custom_timesheet",
        "Salary Slip-custom_time_tracker",
        "Salary Slip-custom_time_tracker_hours",
        "Salary Slip-custom_time_tracker_log_count",
        "Salary Slip-custom_hourly_rate",
        "Salary Slip-custom_total_monthly_hours",
        "Salary Slip-custom_total_exceeded_hours",
        f"Salary Slip-{SALARY_SLIP_WEEKLY_SUMMARY_JSON_FIELD}",
    }
    unsafe_cleanup = sorted(set(legacy_custom_fields) & managed_custom_fields)
    if unsafe_cleanup:
        frappe.throw(
            _("Refusing to delete active Time Tracker Custom Fields: {0}.").format(
                ", ".join(frappe.bold(name) for name in unsafe_cleanup)
            )
        )

    for name in legacy_custom_fields:
        if frappe.db.exists("Custom Field", name):
            frappe.delete_doc(
                "Custom Field", name, ignore_permissions=True, force=True
            )

    if frappe.db.exists("Print Format", LEGACY_SALARY_SLIP_PRINT_FORMAT):
        frappe.delete_doc(
            "Print Format",
            LEGACY_SALARY_SLIP_PRINT_FORMAT,
            ignore_permissions=True,
            force=True,
        )

    for doctype in LEGACY_DOCTYPES:
        if not frappe.db.exists("DocType", doctype):
            continue
        try:
            frappe.delete_doc(
                "DocType", doctype, ignore_permissions=True, force=True
            )
        except Exception:
            frappe.log_error(
                message=frappe.get_traceback(),
                title=f"Time Tracker legacy DocType cleanup: {doctype}",
            )

    remaining_legacy_doctypes = [
        doctype
        for doctype in LEGACY_DOCTYPES
        if frappe.db.exists("DocType", doctype)
    ]
    if remaining_legacy_doctypes:
        frappe.throw(
            _("Could not remove legacy Time Tracker DocTypes: {0}.").format(
                ", ".join(
                    frappe.bold(doctype)
                    for doctype in remaining_legacy_doctypes
                )
            )
        )

    for doctype in ("Salary Structure", "Payroll Entry", "Salary Slip"):
        frappe.clear_cache(doctype=doctype)


def reconcile_employee_weekly_limit_aliases() -> int:
    if not frappe.db.table_exists("Employee"):
        return 0

    available_fields = [
        fieldname
        for fieldname in EMPLOYEE_WEEKLY_LIMIT_FIELDS
        if frappe.db.has_column("Employee", fieldname)
    ]
    if len(available_fields) < 2:
        return 0

    employees = frappe.get_all(
        "Employee",
        fields=["name", *available_fields],
        limit_page_length=0,
    )
    updated = 0
    for employee in employees:
        settings = resolve_employee_weekly_hours_limit(
            employee, available_fields=available_fields
        )
        weekly_limit = flt(settings.weekly_limit, 3)
        values = {
            fieldname: weekly_limit
            for fieldname in available_fields
            if abs(flt(employee.get(fieldname)) - weekly_limit) > 0.000001
        }
        if values:
            frappe.db.set_value(
                "Employee", employee.name, values, update_modified=False
            )
            updated += 1
    return updated


def _synchronise_managed_custom_field_properties(
    custom_fields: dict[str, list[dict[str, object]]],
) -> None:
    managed_properties = (
        "label",
        "description",
        "options",
        "hidden",
        "read_only",
        "insert_after",
        "depends_on",
        "default",
        "precision",
        "allow_on_submit",
        "no_copy",
    )
    for doctype, field_definitions in custom_fields.items():
        for definition in field_definitions:
            fieldname = str(definition.get("fieldname") or "").strip()
            if not fieldname:
                continue
            custom_field_name = frappe.db.get_value(
                "Custom Field", {"dt": doctype, "fieldname": fieldname}, "name"
            )
            if not custom_field_name:
                continue
            values = {
                property_name: definition[property_name]
                for property_name in managed_properties
                if property_name in definition
            }
            if values:
                frappe.db.set_value(
                    "Custom Field",
                    custom_field_name,
                    values,
                    update_modified=False,
                )


def _doctype_has_field(doctype: str, fieldname: str) -> bool:
    if not frappe.db.exists("DocType", doctype):
        return False
    frappe.clear_cache(doctype=doctype)
    if frappe.get_meta(doctype, cached=False).has_field(fieldname):
        return True
    return bool(
        frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname})
    )


def _create_custom_field_if_missing(
    doctype: str,
    field_definition: dict[str, object],
) -> None:
    fieldname = str(field_definition.get("fieldname") or "").strip()
    if not fieldname or not frappe.db.exists("DocType", doctype):
        return
    frappe.clear_cache(doctype=doctype)
    if frappe.get_meta(doctype, cached=False).has_field(fieldname):
        return
    if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
        frappe.clear_cache(doctype=doctype)
        return
    create_custom_field(doctype, field_definition, ignore_validate=False)
    frappe.clear_cache(doctype=doctype)


def _migrate_legacy_payroll_fields() -> None:
    if not frappe.db.exists("DocType", "Payroll Entry"):
        return

    if (
        frappe.db.has_column("Payroll Entry", "custom_time_tracking_source")
        and frappe.db.has_column("Payroll Entry", "custom_pay_using_time_tracker")
    ):
        frappe.db.sql(
            """
            UPDATE `tabPayroll Entry`
            SET custom_pay_using_time_tracker = 1
            WHERE custom_time_tracking_source = 'Time Tracker'
              AND IFNULL(custom_pay_using_time_tracker, 0) = 0
            """
        )


def repair_tracker_log_ticket_doctype() -> None:
    """Populate the Dynamic Link discriminator added for optional Helpdesk."""

    if not (
        frappe.db.table_exists("Tracker Log")
        and frappe.db.has_column("Tracker Log", "ticket_doctype")
    ):
        return

    frappe.db.sql(
        """
        UPDATE `tabTracker Log`
        SET ticket_doctype = %s
        WHERE IFNULL(ticket_doctype, '') = ''
        """,
        ("HD Ticket",),
    )


def repair_tracker_log_payroll_fields() -> int:
    """Backfill payroll-critical values on Tracker Logs created by old builds.

    Current Tracker Log validation writes ``log_date`` from ``start_time`` and
    computes ``hours`` when a timer stops. Older records can predate those
    safeguards. Payroll intentionally ignores rows without a date or positive
    duration, so migration repairs only missing/invalid derived values and
    leaves every positive manually-corrected duration unchanged.
    """

    if not (
        frappe.db.exists("DocType", "Tracker Log")
        and frappe.db.has_column("Tracker Log", "log_date")
        and frappe.db.has_column("Tracker Log", "start_time")
        and frappe.db.has_column("Tracker Log", "end_time")
        and frappe.db.has_column("Tracker Log", "hours")
        and frappe.db.has_column("Tracker Log", "status")
    ):
        return 0

    missing_dates = frappe.db.sql(
        """
        SELECT COUNT(*)
        FROM `tabTracker Log`
        WHERE start_time IS NOT NULL
          AND log_date IS NULL
        """
    )[0][0]
    invalid_hours = frappe.db.sql(
        """
        SELECT COUNT(*)
        FROM `tabTracker Log`
        WHERE status = 'Stopped'
          AND start_time IS NOT NULL
          AND end_time IS NOT NULL
          AND end_time > start_time
          AND COALESCE(hours, 0) <= 0
        """
    )[0][0]

    if missing_dates:
        frappe.db.sql(
            """
            UPDATE `tabTracker Log`
            SET log_date = DATE(start_time)
            WHERE start_time IS NOT NULL
              AND log_date IS NULL
            """
        )

    if invalid_hours:
        frappe.db.sql(
            """
            UPDATE `tabTracker Log`
            SET hours = TIMESTAMPDIFF(MICROSECOND, start_time, end_time)
                / 3600000000.0
            WHERE status = 'Stopped'
              AND start_time IS NOT NULL
              AND end_time IS NOT NULL
              AND end_time > start_time
              AND COALESCE(hours, 0) <= 0
            """
        )

    return int(missing_dates or 0) + int(invalid_hours or 0)


def repair_draft_time_tracker_salary_slips() -> int:
    """Revalidate and allocate draft slips created by Time Tracker payroll.

    Older builds could save a normal/fixed Salary Structure or a legacy Draft
    Timesheet source. Version 0.5.0 also starts reserving Tracker Logs while the
    Salary Slip is still Draft. Each draft is repaired in its own savepoint so a
    site-specific validation problem is logged without blocking migration.
    Submitted salary amounts are never changed here.
    """

    required_columns = (
        frappe.db.has_column("Payroll Entry", "custom_pay_using_time_tracker")
        and frappe.db.has_column("Salary Slip", "custom_time_tracking_source")
    )
    if not required_columns:
        return 0

    payroll_entries = frappe.get_all(
        "Payroll Entry",
        filters={"custom_pay_using_time_tracker": 1},
        pluck="name",
        limit_page_length=0,
    )
    if not payroll_entries:
        return 0

    drafts = frappe.get_all(
        "Salary Slip",
        filters={
            "docstatus": 0,
            "payroll_entry": ["in", payroll_entries],
        },
        pluck="name",
        limit_page_length=0,
    )

    repaired = 0
    for salary_slip_name in drafts:
        savepoint = "time_tracker_repair_draft_salary_slip"
        frappe.db.savepoint(savepoint)
        try:
            salary_slip = frappe.get_doc("Salary Slip", salary_slip_name)
            salary_slip.custom_time_tracking_source = "Time Tracker"
            salary_slip.salary_slip_based_on_timesheet = 1

            if salary_slip.meta.has_field("custom_payroll_hours_source"):
                salary_slip.custom_payroll_hours_source = "Tracker Log"
            if salary_slip.meta.has_field("custom_timesheet"):
                salary_slip.custom_timesheet = None

            salary_slip.flags.ignore_permissions = True
            salary_slip.save(ignore_permissions=True)
            repaired += 1
        except Exception:
            frappe.db.rollback(save_point=savepoint)
            frappe.log_error(
                message=frappe.get_traceback(),
                title=f"Time Tracker draft Salary Slip repair: {salary_slip_name}",
            )

    return repaired


def repair_time_tracker_salary_slip_links() -> dict[str, int]:
    """Reconcile live Time Tracker Salary Slips after an app upgrade.

    Draft slips are recalculated by ``repair_draft_time_tracker_salary_slips``
    first. This second pass also restores missing links for submitted slips
    without changing Salary Slip amounts or status.
    """

    totals = {
        "payroll_entries": 0,
        "salary_slips": 0,
        "reconciled": 0,
        "linked_tracker_logs": 0,
        "linked_tracker_hours": 0.0,
        "errors": 0,
    }

    if not (
        frappe.db.has_column("Payroll Entry", "custom_pay_using_time_tracker")
        and frappe.db.has_column("Tracker Log", "salary_slip")
    ):
        return totals

    payroll_entries = frappe.get_all(
        "Payroll Entry",
        filters={
            "custom_pay_using_time_tracker": 1,
            "docstatus": ["!=", 2],
        },
        pluck="name",
        limit_page_length=0,
    )

    for payroll_entry in payroll_entries:
        result = reconcile_time_tracker_salary_slips_for_payroll_entry(
            payroll_entry,
        )
        totals["payroll_entries"] += 1
        totals["salary_slips"] += int(result.salary_slips or 0)
        totals["reconciled"] += int(result.reconciled or 0)
        totals["linked_tracker_logs"] += int(
            result.linked_tracker_logs or 0
        )
        totals["linked_tracker_hours"] += flt(
            result.linked_tracker_hours or 0,
            6,
        )
        totals["errors"] += len(result.errors or [])

    return totals
