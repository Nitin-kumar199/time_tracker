from __future__ import annotations

from datetime import datetime, time
from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, get_first_day, getdate, now_datetime

from time_tracker.api import (
    _add_context_display_names,
    _context_field_permissions,
    _hide_disallowed_context_fields,
)
from time_tracker.permissions import (
    ROLE_HR_MANAGER,
    ROLE_LOG_EDITOR,
    ROLE_MANAGER,
    is_system_manager,
    visible_employees,
)


def execute(filters: dict[str, Any] | None = None):
    filters = frappe._dict(filters or {})
    _validate_access()

    today = getdate(now_datetime())
    from_date = getdate(filters.from_date) if filters.from_date else get_first_day(today)
    to_date = getdate(filters.to_date) if filters.to_date else today

    if from_date > to_date:
        frappe.throw(_("From Date cannot be after To Date."))

    columns = _get_columns()
    data = _get_data(filters, from_date, to_date)
    chart = _get_chart(data, from_date, to_date)
    summary = _get_summary(data)

    return columns, data, None, chart, summary


def _validate_access() -> None:
    roles = set(frappe.get_roles(frappe.session.user))

    if not (
        is_system_manager(frappe.session.user)
        or ROLE_MANAGER in roles
        or ROLE_LOG_EDITOR in roles
        or ROLE_HR_MANAGER in roles
    ):
        frappe.throw(
            _("You are not permitted to open the Time Tracker Management report."),
            frappe.PermissionError,
        )


def _get_columns() -> list[dict[str, Any]]:
    return [
        {
            "label": _("Employee"),
            "fieldname": "employee",
            "fieldtype": "Link",
            "options": "Employee",
            "width": 125,
        },
        {
            "label": _("Employee Name"),
            "fieldname": "employee_name",
            "fieldtype": "Data",
            "width": 180,
        },
        {
            "label": _("Employee Status"),
            "fieldname": "employee_status",
            "fieldtype": "Data",
            "width": 115,
        },
        {
            "label": _("Tracker Status"),
            "fieldname": "tracker_status",
            "fieldtype": "Data",
            "width": 115,
        },
        {
            "label": _("Timer Status"),
            "fieldname": "timer_status",
            "fieldtype": "Data",
            "width": 105,
        },
        {
            "label": _("Active Since"),
            "fieldname": "active_since",
            "fieldtype": "Datetime",
            "width": 150,
        },
        {
            "label": _("Current Work"),
            "fieldname": "current_context",
            "fieldtype": "Data",
            "width": 240,
        },
        {
            "label": _("Today Hours"),
            "fieldname": "today_hours",
            "fieldtype": "Float",
            "precision": 2,
            "width": 105,
        },
        {
            "label": _("Period Hours"),
            "fieldname": "period_hours",
            "fieldtype": "Float",
            "precision": 2,
            "width": 110,
        },
        {
            "label": _("Completed Logs"),
            "fieldname": "completed_logs",
            "fieldtype": "Int",
            "width": 115,
        },
        {
            "label": _("Last Activity"),
            "fieldname": "last_activity",
            "fieldtype": "Datetime",
            "width": 150,
        },
        {
            "label": _("Department"),
            "fieldname": "department",
            "fieldtype": "Link",
            "options": "Department",
            "width": 150,
        },
        {
            "label": _("Designation"),
            "fieldname": "designation",
            "fieldtype": "Link",
            "options": "Designation",
            "width": 140,
        },
        {
            "label": _("Company"),
            "fieldname": "company",
            "fieldtype": "Link",
            "options": "Company",
            "width": 160,
        },
        {
            "label": _("Time Tracker"),
            "fieldname": "time_tracker",
            "fieldtype": "Link",
            "options": "Time Tracker",
            "width": 135,
        },
    ]


def _get_data(filters, from_date, to_date) -> list[dict[str, Any]]:
    permitted = visible_employees(frappe.session.user)

    if permitted is not None and not permitted:
        return []

    employee_filters: dict[str, Any] = {}

    if permitted is not None:
        employee_filters["name"] = ["in", sorted(permitted)]

    for fieldname in ("company", "department"):
        if filters.get(fieldname):
            employee_filters[fieldname] = filters.get(fieldname)

    if filters.employee_status:
        employee_filters["status"] = filters.employee_status

    if filters.employee:
        if permitted is not None and filters.employee not in permitted:
            return []
        employee_filters["name"] = filters.employee

    employees = frappe.get_all(
        "Employee",
        filters=employee_filters,
        fields=[
            "name",
            "employee_name",
            "status",
            "company",
            "department",
            "designation",
        ],
        limit_page_length=0,
    )

    if not employees:
        return []

    employee_map = {row.name: row for row in employees}
    trackers = frappe.get_all(
        "Time Tracker",
        filters={"employee": ["in", list(employee_map)]},
        fields=["name", "employee", "status"],
        limit_page_length=0,
    )
    tracker_by_employee = {row.employee: row for row in trackers}
    tracker_names = [row.name for row in trackers]
    aggregate_map = (
        _get_aggregates(tracker_names, from_date, to_date)
        if tracker_names
        else {}
    )
    running_map = _get_running_logs(tracker_names) if tracker_names else {}
    today = getdate(now_datetime())
    data: list[dict[str, Any]] = []

    for employee in employees:
        tracker = tracker_by_employee.get(employee.name)
        aggregate = (
            aggregate_map.get(tracker.name, frappe._dict())
            if tracker
            else frappe._dict()
        )
        running = running_map.get(tracker.name) if tracker else None
        timer_status = "Running" if running else ("Idle" if tracker else "No Tracker")

        if filters.timer_status and timer_status != filters.timer_status:
            continue

        today_hours = flt(aggregate.get("today_hours"), 6)
        period_hours = flt(aggregate.get("period_hours"), 6)

        if running:
            today_hours += _running_hours_between(
                running.start_time,
                today,
                today,
            )
            period_hours += _running_hours_between(
                running.start_time,
                from_date,
                to_date,
            )

        data.append(
            {
                "employee": employee.name,
                "employee_name": employee.employee_name,
                "employee_status": employee.status,
                "tracker_status": tracker.status if tracker else "No Tracker",
                "timer_status": timer_status,
                "active_since": running.start_time if running else None,
                "current_context": _context_label(running),
                "today_hours": today_hours,
                "period_hours": period_hours,
                "completed_logs": int(aggregate.get("completed_logs") or 0),
                "last_activity": aggregate.get("last_activity"),
                "department": employee.department,
                "designation": employee.designation,
                "company": employee.company,
                "time_tracker": tracker.name if tracker else None,
            }
        )

    timer_order = {"Running": 0, "Idle": 1, "No Tracker": 2}
    return sorted(
        data,
        key=lambda row: (
            timer_order.get(row["timer_status"], 9),
            -flt(row["period_hours"]),
            row["employee_name"] or row["employee"],
        ),
    )


def _running_hours_between(start_time, from_date, to_date) -> float:
    if not start_time:
        return 0.0

    started_at = get_datetime(start_time)
    period_start = datetime.combine(getdate(from_date), time.min)
    period_end = datetime.combine(getdate(to_date), time.max)
    overlap_start = max(started_at, period_start)
    overlap_end = min(now_datetime(), period_end)

    if overlap_end <= overlap_start:
        return 0.0

    return flt((overlap_end - overlap_start).total_seconds() / 3600, 6)


def _get_aggregates(tracker_names, from_date, to_date) -> dict[str, frappe._dict]:
    placeholders = ", ".join(["%s"] * len(tracker_names))
    today = getdate(now_datetime())
    rows = frappe.db.sql(
        f"""
        SELECT
            time_tracker,
            COALESCE(SUM(
                CASE
                    WHEN status = 'Stopped' AND log_date BETWEEN %s AND %s
                    THEN hours ELSE 0
                END
            ), 0) AS period_hours,
            COALESCE(SUM(
                CASE
                    WHEN status = 'Stopped' AND log_date = %s
                    THEN hours ELSE 0
                END
            ), 0) AS today_hours,
            COALESCE(SUM(
                CASE
                    WHEN status = 'Stopped' AND log_date BETWEEN %s AND %s
                    THEN 1 ELSE 0
                END
            ), 0) AS completed_logs,
            MAX(COALESCE(end_time, start_time)) AS last_activity
        FROM `tabTracker Log`
        WHERE time_tracker IN ({placeholders})
        GROUP BY time_tracker
        """,
        [from_date, to_date, today, from_date, to_date, *tracker_names],
        as_dict=True,
    )
    return {row.time_tracker: row for row in rows}


def _get_running_logs(tracker_names) -> dict[str, frappe._dict]:
    rows = frappe.get_all(
        "Tracker Log",
        filters={
            "time_tracker": ["in", tracker_names],
            "status": "Running",
        },
        fields=[
            "name",
            "time_tracker",
            "start_time",
            "project",
            "task",
            "ticket",
        ],
        order_by="creation desc",
        limit_page_length=0,
    )

    permissions = _context_field_permissions()
    rows = _hide_disallowed_context_fields(rows, permissions)
    rows = _add_context_display_names(rows, permissions)

    result: dict[str, frappe._dict] = {}
    for row in rows:
        result.setdefault(row.time_tracker, frappe._dict(row))
    return result


def _context_label(log) -> str:
    if not log:
        return ""

    parts = []

    if log.get("project"):
        parts.append(log.get("project_name") or log.project)
    if log.get("task"):
        parts.append(log.get("task_name") or log.task)
    if log.get("ticket"):
        parts.append(log.ticket)

    if parts:
        return " / ".join(parts)

    if log.get("context_restricted"):
        return _("Restricted work item")

    return ""


def _get_chart(data, from_date, to_date):
    top_rows = sorted(data, key=lambda row: flt(row["period_hours"]), reverse=True)[:15]

    return {
        "data": {
            "labels": [row["employee_name"] or row["employee"] for row in top_rows],
            "datasets": [
                {
                    "name": _("Hours"),
                    "values": [flt(row["period_hours"], 2) for row in top_rows],
                }
            ],
        },
        "type": "bar",
        "height": 280,
        "colors": ["#5e64ff"],
        "axisOptions": {"xIsSeries": 1},
        "title": _("Tracked Hours from {0} to {1}").format(from_date, to_date),
    }


def _get_summary(data):
    return [
        {
            "label": _("Employees"),
            "value": len(data),
            "indicator": "Blue",
            "datatype": "Int",
        },
        {
            "label": _("Time Trackers"),
            "value": sum(1 for row in data if row.get("time_tracker")),
            "indicator": "Blue",
            "datatype": "Int",
        },
        {
            "label": _("Running Timers"),
            "value": sum(1 for row in data if row["timer_status"] == "Running"),
            "indicator": "Green",
            "datatype": "Int",
        },
        {
            "label": _("Period Hours"),
            "value": flt(sum(flt(row["period_hours"]) for row in data), 2),
            "indicator": "Blue",
            "datatype": "Float",
        },
    ]
