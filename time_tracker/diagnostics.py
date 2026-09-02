from __future__ import annotations

from pathlib import Path
from typing import Any

import frappe

import time_tracker
from time_tracker import hooks
from time_tracker.permissions import (
    get_employees_for_user,
    get_reportees,
    visible_employees,
)


def get_deployment_status() -> dict[str, Any]:
    """Return a read-only source, schema, and Desk-asset deployment check."""

    package_file = Path(time_tracker.__file__).resolve()
    package_dir = package_file.parent
    app_root = package_dir.parent

    required_columns = {
        "Time Tracker.enable_browser_widget": _has_column(
            "Time Tracker", "enable_browser_widget"
        ),
        "Tracker Log.description": _has_column("Tracker Log", "description"),
        "Tracker Log.ticket_doctype": _has_column("Tracker Log", "ticket_doctype"),
        "Tracker Log.salary_slip": _has_column("Tracker Log", "salary_slip"),
        "Time Tracker Correction Request.workflow_state": _has_column(
            "Time Tracker Correction Request", "workflow_state"
        ),
        "Time Tracker Setting.company": _has_column(
            "Time Tracker Setting", "company"
        ),
    }

    source_assets = {
        "widget_js": package_dir / "public" / "js" / "time_tracker_widget.js",
        "widget_css": package_dir / "public" / "css" / "time_tracker_widget.css",
        "time_tracker_form_js": (
            package_dir
            / "time_tracker"
            / "doctype"
            / "time_tracker"
            / "time_tracker.js"
        ),
    }

    return {
        "time_tracker_version": time_tracker.__version__,
        "active_python_package": str(package_file),
        "active_app_root": str(app_root),
        "app_installed_on_site": "time_tracker" in frappe.get_installed_apps(),
        "nested_full_app_detected_inside_python_package": bool(
            (package_dir / "pyproject.toml").exists()
            and (package_dir / "time_tracker" / "__init__.py").exists()
        ),
        "schema_ready": all(required_columns.values()),
        "required_columns": required_columns,
        "desk_assets_declared": {
            "js": list(getattr(hooks, "app_include_js", []) or []),
            "css": list(getattr(hooks, "app_include_css", []) or []),
        },
        "source_assets_exist": {
            name: path.exists() for name, path in source_assets.items()
        },
        "source_asset_paths": {
            name: str(path) for name, path in source_assets.items()
        },
        "automatic_time_tracker_backfill_enabled": False,
    }


def get_installation_status(user: str | None = None) -> dict[str, Any]:
    """Return deployment status plus manager/reportee Time Tracker scope.

    Run from the bench directory with::

        bench --site <site> execute time_tracker.diagnostics.get_installation_status \
            --kwargs '{"user": "manager@example.com"}'

    The function intentionally creates or changes no documents.
    """

    user = user or frappe.session.user
    result = get_deployment_status()

    linked_employees = get_employees_for_user(user, active_only=False)
    reportees_by_manager = {
        employee: sorted(get_reportees(employee)) for employee in linked_employees
    }
    permitted = visible_employees(user)

    tracker_filters: dict[str, Any] = {}
    if permitted is not None:
        tracker_filters["employee"] = ["in", sorted(permitted)] if permitted else ["in", []]

    trackers = []
    tracker_table_ready = bool(
        frappe.db.exists("DocType", "Time Tracker")
        and frappe.db.table_exists("Time Tracker")
    )
    widget_column_ready = _has_column("Time Tracker", "enable_browser_widget")

    if tracker_table_ready and (permitted is None or permitted):
        tracker_fields = ["name", "employee", "employee_name", "status"]
        if widget_column_ready:
            tracker_fields.append("enable_browser_widget")
        trackers = frappe.get_all(
            "Time Tracker",
            filters=tracker_filters,
            fields=tracker_fields,
            order_by="employee asc",
            limit_page_length=0,
        )

    result.update(
        {
            "checked_user": user,
            "roles": frappe.get_roles(user),
            "linked_employees": linked_employees,
            "reportees_by_manager_employee": reportees_by_manager,
            "visible_employees": None if permitted is None else sorted(permitted),
            "existing_visible_trackers": [dict(row) for row in trackers],
        }
    )
    return result


def _has_column(doctype: str, fieldname: str) -> bool:
    return bool(
        frappe.db.table_exists(doctype)
        and frappe.db.has_column(doctype, fieldname)
    )
