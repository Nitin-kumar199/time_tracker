from __future__ import annotations

import frappe
from frappe import _


TRACKER_DOCTYPE = "Time Tracker"
TRACKER_EMPLOYEE_FIELD = "employee"


def get_dashboard_data(data=None):
    """Add Time Tracker to the Employee form's Connections dashboard."""

    data = frappe._dict(data or {})
    transactions = data.setdefault("transactions", [])
    non_standard_fieldnames = data.setdefault(
        "non_standard_fieldnames",
        {},
    )
    non_standard_fieldnames[TRACKER_DOCTYPE] = TRACKER_EMPLOYEE_FIELD

    for group in transactions:
        items = group.setdefault("items", [])

        if TRACKER_DOCTYPE not in items:
            continue

        group.setdefault("fieldnames", {})[
            TRACKER_DOCTYPE
        ] = TRACKER_EMPLOYEE_FIELD
        return data

    transactions.append(
        {
            "label": _("Time Tracking"),
            "items": [TRACKER_DOCTYPE],
            "fieldnames": {
                TRACKER_DOCTYPE: TRACKER_EMPLOYEE_FIELD,
            },
        }
    )

    return data
