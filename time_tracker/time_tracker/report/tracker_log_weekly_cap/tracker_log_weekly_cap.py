from __future__ import annotations

import calendar
from collections import OrderedDict
from datetime import date, datetime, time, timedelta
from typing import Any

import frappe
from frappe import _
from frappe.utils import flt

from time_tracker.permissions import visible_employees


MONTHS = {
    month_name: month_number
    for month_number, month_name in enumerate(calendar.month_name)
    if month_name
}


def execute(filters: dict[str, Any] | None = None):
    filters = frappe._dict(filters or {})
    employee, month_number, year = _validate_filters(filters)

    period_start = date(year, month_number, 1)
    period_end = date(year, month_number, calendar.monthrange(year, month_number)[1])

    weekly_cap = flt(
        frappe.db.get_value(
            "Employee",
            employee,
            "custom_working_hours_weekly_limit",
        )
        or 25
    )

    logged_by_date = _get_logged_hours(employee, period_start, period_end)
    periods = _build_periods(period_start, period_end)

    columns = _get_columns()
    data = _build_data(periods, logged_by_date, weekly_cap)

    return columns, data


def _validate_filters(filters):
    employee = filters.get("employee")
    month = filters.get("month")
    year = filters.get("year")

    if not employee:
        frappe.throw(_("Employee is required."))

    if not month or month not in MONTHS:
        frappe.throw(_("Please select a valid Month."))

    try:
        year = int(year)
    except (TypeError, ValueError):
        frappe.throw(_("Please enter a valid Year."))

    if year < 1900 or year > 9999:
        frappe.throw(_("Please enter a valid Year."))

    if not frappe.db.exists("Employee", employee):
        frappe.throw(_("Employee {0} does not exist.").format(frappe.bold(employee)))

    permitted = visible_employees(frappe.session.user)
    if permitted is not None and employee not in permitted:
        frappe.throw(
            _("You are not permitted to view Tracker Log data for this employee."),
            frappe.PermissionError,
        )

    return employee, MONTHS[month], year


def _get_columns():
    return [
        {
            "label": _("Weekly Cap (h)"),
            "fieldname": "weekly_cap",
            "fieldtype": "Float",
            "precision": 4,
            "width": 100,
        },
        {
            "label": _("Period"),
            "fieldname": "period",
            "fieldtype": "Data",
            "width": 90,
        },
        {
            "label": _("Period Start"),
            "fieldname": "period_start",
            "fieldtype": "Data",
            "width": 130,
        },
        {
            "label": _("Period End"),
            "fieldname": "period_end",
            "fieldtype": "Data",
            "width": 130,
        },
        {
            "label": _("Days in Period"),
            "fieldname": "days_in_period",
            "fieldtype": "Int",
            "width": 110,
        },
        {
            "label": _("Daily Cap (h/day)"),
            "fieldname": "daily_cap",
            "fieldtype": "Float",
            "precision": 4,
            "width": 120,
        },
        {
            "label": _("Period Cap (h)"),
            "fieldname": "period_cap",
            "fieldtype": "Float",
            "precision": 4,
            "width": 120,
        },
        {
            "label": _("Logged Hours"),
            "fieldname": "logged_hours",
            "fieldtype": "Float",
            "precision": 4,
            "width": 120,
        },
        {
            "label": _("Payable Hours"),
            "fieldname": "payable_hours",
            "fieldtype": "Float",
            "precision": 4,
            "width": 120,
        },
        {
            "label": _("Exceeded Hours"),
            "fieldname": "exceeded_hours",
            "fieldtype": "Float",
            "precision": 4,
            "width": 120,
        },
    ]


def _get_logged_hours(employee: str, from_date: date, to_date: date) -> dict[date, float]:
    rows = frappe.db.sql(
        """
        SELECT
            log_date,
            SUM(IFNULL(hours, 0)) AS logged_hours
        FROM `tabTracker Log`
        WHERE
            employee = %(employee)s
            AND status = 'Stopped'
            AND log_date BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY log_date
        ORDER BY log_date
        """,
        {
            "employee": employee,
            "from_date": from_date,
            "to_date": to_date,
        },
        as_dict=True,
    )

    return {
        row.log_date: flt(row.logged_hours)
        for row in rows
        if row.log_date
    }


def _build_periods(from_date: date, to_date: date):
    """
    Split the selected month into Monday-based calendar weeks.

    The first and last periods may be partial weeks, matching the original
    Query Report behaviour.
    """
    groups: OrderedDict[date, list[date]] = OrderedDict()

    current = from_date
    while current <= to_date:
        monday = current - timedelta(days=current.weekday())
        groups.setdefault(monday, []).append(current)
        current += timedelta(days=1)

    periods = []
    for index, dates in enumerate(groups.values(), start=1):
        periods.append(
            {
                "period": _("Week {0}").format(index),
                "period_start": dates[0],
                "period_end": dates[-1],
                "days_in_period": len(dates),
            }
        )

    return periods


def _build_data(periods, logged_by_date, weekly_cap: float):
    daily_cap = weekly_cap / 7
    data = []

    for period in periods:
        logged_hours = 0.0
        current = period["period_start"]

        while current <= period["period_end"]:
            logged_hours += flt(logged_by_date.get(current, 0))
            current += timedelta(days=1)

        period_cap = daily_cap * period["days_in_period"]
        payable_hours = min(logged_hours, period_cap)
        exceeded_hours = max(0, logged_hours - period_cap)

        data.append(
            {
                "weekly_cap": round(weekly_cap, 4),
                "period": period["period"],
                "period_start": _format_period_start(period["period_start"]),
                "period_end": _format_period_end(period["period_end"]),
                "days_in_period": period["days_in_period"],
                "daily_cap": round(daily_cap, 4),
                "period_cap": round(period_cap, 4),
                "logged_hours": round(logged_hours, 4),
                "payable_hours": round(payable_hours, 4),
                "exceeded_hours": round(exceeded_hours, 4),
            }
        )

    return data


def _format_period_start(value: date) -> str:
    # Same visual format as DATE_FORMAT(..., '%c/%e/%Y 0:00')
    return f"{value.month}/{value.day}/{value.year} 0:00"


def _format_period_end(value: date) -> str:
    # Same visual format as DATE_FORMAT(..., '%c/%e/%Y 23:59')
    return f"{value.month}/{value.day}/{value.year} 23:59"
