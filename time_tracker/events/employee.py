from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt

from time_tracker.time_tracker_security import employee_tracker_sync


TRACKER_DOCTYPE = "Time Tracker"


def sync_employee_weekly_hours_limit(doc, method: str | None = None) -> None:
    """Keep the visible weekly limit and its compatibility alias identical."""

    del method

    from time_tracker.payroll import (
        DEFAULT_WEEKLY_HOURS_LIMIT,
        EMPLOYEE_WEEKLY_LIMIT_FIELDS,
        resolve_employee_weekly_hours_limit,
    )

    available_fields = [
        fieldname
        for fieldname in EMPLOYEE_WEEKLY_LIMIT_FIELDS
        if doc.meta.has_field(fieldname)
    ]
    if len(available_fields) < 2:
        return

    changed_fields = []
    has_value_changed = getattr(doc, "has_value_changed", None)
    if callable(has_value_changed):
        changed_fields = [
            fieldname
            for fieldname in available_fields
            if has_value_changed(fieldname)
        ]

    if len(changed_fields) == 1:
        changed_value = flt(doc.get(changed_fields[0]))
        weekly_limit = (
            changed_value
            if changed_value > 0
            else DEFAULT_WEEKLY_HOURS_LIMIT
        )
    else:
        settings = resolve_employee_weekly_hours_limit(
            doc,
            available_fields=available_fields,
        )
        weekly_limit = settings.weekly_limit

    weekly_limit = flt(weekly_limit, 3)
    for fieldname in available_fields:
        if abs(flt(doc.get(fieldname)) - weekly_limit) > 0.000001:
            doc.set(fieldname, weekly_limit)


def handle_employee_change(doc, method: str | None = None) -> None:
    """Synchronise Employee-derived values on an existing Time Tracker."""

    sync_time_tracker_status(doc, method)


def ensure_time_tracker_for_employee(employee: Any) -> str | None:
    """Create the Employee's permanent tracker idempotently.

    Time Tracker onboarding calls this only when the effective submitted Salary
    Structure uses Time Tracker payroll (``custom_based_on_time_tracker`` is enabled). It is
    also safe to invoke manually for a specific Employee.
    """

    if not _time_tracker_schema_ready():
        return None

    values = _employee_values(employee)
    if not values.name:
        return None

    existing = frappe.db.get_value(
        TRACKER_DOCTYPE,
        {"employee": values.name},
        "name",
    )
    if existing:
        return existing

    tracker = frappe.get_doc(
        {
            "doctype": TRACKER_DOCTYPE,
            "employee": values.name,
            "employee_name": values.employee_name or values.name,
            "status": values.status or "Inactive",
            "enable_browser_widget": 1,
        }
    )

    try:
        with employee_tracker_sync():
            tracker.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        # Explicit administrative automation may race. The unique Employee
        # field is the final guard, so treat the winner as success.
        existing = frappe.db.get_value(
            TRACKER_DOCTYPE,
            {"employee": values.name},
            "name",
        )
        if existing:
            return existing
        raise

    return tracker.name


def ensure_time_trackers_for_employees(*, company: str | None = None) -> int:
    """Manual bulk utility that creates missing trackers without committing."""

    if not _time_tracker_schema_ready():
        return 0

    filters = {"company": company} if company else None
    employees = frappe.get_all(
        "Employee",
        filters=filters,
        fields=["name", "employee_name", "status"],
        order_by="name",
        limit_page_length=0,
    )

    if not employees:
        return 0

    existing_employees = set(
        frappe.get_all(
            TRACKER_DOCTYPE,
            filters={"employee": ["in", [row.name for row in employees]]},
            pluck="employee",
            limit_page_length=0,
        )
    )

    created = 0
    for employee in employees:
        if employee.name in existing_employees:
            continue

        if ensure_time_tracker_for_employee(employee):
            created += 1
            existing_employees.add(employee.name)

    return created


def sync_time_tracker_status(doc, method: str | None = None) -> None:
    """Keep Employee-derived values on the linked Time Tracker in sync."""

    del method

    if not _time_tracker_schema_ready():
        return

    values = _employee_values(doc)
    if not values.name:
        return

    tracker = frappe.db.get_value(
        TRACKER_DOCTYPE,
        {"employee": values.name},
        ["name", "status", "employee", "employee_name"],
        as_dict=True,
    )

    if not tracker:
        return

    expected = {
        "status": values.status or "Inactive",
        "employee_name": values.employee_name or values.name,
    }
    changed = {
        fieldname: value
        for fieldname, value in expected.items()
        if tracker.get(fieldname) != value
    }

    if changed:
        frappe.db.set_value(
            TRACKER_DOCTYPE,
            tracker.name,
            changed,
            update_modified=False,
        )


def sync_all_time_tracker_statuses() -> int:
    """Synchronise Employee status and name on trackers that already exist."""

    if not _time_tracker_schema_ready():
        return 0

    trackers = frappe.get_all(
        TRACKER_DOCTYPE,
        fields=["name", "employee", "status", "employee_name"],
        limit_page_length=0,
    )

    employee_ids = {tracker.employee for tracker in trackers if tracker.employee}
    if not employee_ids:
        return 0

    employees = {
        employee.name: employee
        for employee in frappe.get_all(
            "Employee",
            filters={"name": ["in", sorted(employee_ids)]},
            fields=["name", "employee_name", "status"],
            limit_page_length=0,
        )
    }

    updated = 0
    for tracker in trackers:
        employee = employees.get(tracker.employee)
        if not employee:
            continue

        expected = {
            "status": employee.status or "Inactive",
            "employee_name": employee.employee_name or employee.name,
        }
        changed = {
            fieldname: value
            for fieldname, value in expected.items()
            if tracker.get(fieldname) != value
        }
        if not changed:
            continue

        frappe.db.set_value(
            TRACKER_DOCTYPE,
            tracker.name,
            changed,
            update_modified=False,
        )
        updated += 1

    return updated


def _employee_values(employee: Any) -> frappe._dict:
    if isinstance(employee, str):
        return frappe.db.get_value(
            "Employee",
            employee,
            ["name", "employee_name", "status"],
            as_dict=True,
        ) or frappe._dict()

    if hasattr(employee, "get"):
        name = employee.get("name")
        employee_name = employee.get("employee_name")
        status = employee.get("status")
    else:
        name = getattr(employee, "name", None)
        employee_name = getattr(employee, "employee_name", None)
        status = getattr(employee, "status", None)

    if name and (employee_name is None or status is None):
        stored = frappe.db.get_value(
            "Employee",
            name,
            ["employee_name", "status"],
            as_dict=True,
        ) or frappe._dict()
        employee_name = employee_name or stored.get("employee_name")
        status = status or stored.get("status")

    return frappe._dict(
        name=name,
        employee_name=employee_name,
        status=status,
    )


def _time_tracker_table_exists() -> bool:
    return bool(frappe.db.table_exists(TRACKER_DOCTYPE))


def _time_tracker_schema_ready() -> bool:
    return (
        _time_tracker_table_exists()
        and frappe.db.has_column(TRACKER_DOCTYPE, "employee")
        and frappe.db.has_column(TRACKER_DOCTYPE, "status")
        and frappe.db.has_column(TRACKER_DOCTYPE, "employee_name")
    )
