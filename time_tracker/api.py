from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Any

import frappe
from pytz import UnknownTimeZoneError, timezone
from frappe import _
from frappe.utils import (
    add_days,
    cint,
    flt,
    get_datetime,
    getdate,
    get_system_timezone,
    now_datetime,
    time_diff_in_seconds,
)

from time_tracker.permission_utils import (
    doctype_exists,
    has_document_permission,
    has_doctype_permission,
)
from time_tracker.permissions import (
    can_read_employee,
    get_employee_for_user,
    get_employees_for_user,
    is_system_manager,
)
from time_tracker.payroll import (
    EMPLOYEE_WEEKLY_LIMIT_FIELDS,
    resolve_employee_weekly_hours_limit,
)
from time_tracker.settings import get_time_tracker_ui_settings
from time_tracker.time_tracker_security import time_tracker_log_write
from time_tracker.work_context import validate_task_project


TRACKER_DOCTYPE = "Time Tracker"
LOG_DOCTYPE = "Tracker Log"
BROWSER_WIDGET_FIELD = "enable_browser_widget"

# Frappe Helpdesk is optional. Ticket controls are enabled only when the
# ``HD Ticket`` DocType exists and the current user may select its records.
TICKET_DOCTYPE = "HD Ticket"

# Keep enough data for monthly, weekly and heatmap navigation.
HISTORY_DAYS = 730
RECENT_LOG_PAGE_LENGTH = 10
MAX_RECENT_LOG_PAGE_LENGTH = 50
RECENT_LOG_FIELDS = [
    "name",
    "status",
    "log_date",
    "start_time",
    "end_time",
    "hours",
    "project",
    "task",
    "ticket",
    "description",
]


def _normalise_time_zone(time_zone_name: str | None) -> str:
    """Return a valid IANA timezone, falling back to the site timezone."""

    system_time_zone = str(get_system_timezone() or "UTC").strip() or "UTC"
    candidate = str(time_zone_name or system_time_zone).strip() or system_time_zone

    try:
        timezone(candidate)
        return candidate
    except UnknownTimeZoneError:
        try:
            timezone(system_time_zone)
            return system_time_zone
        except UnknownTimeZoneError:
            return "UTC"


def _get_employee_time_zone(employee: str | None) -> str:
    """Return the timezone configured on the User linked to the Employee."""

    if not employee:
        return _normalise_time_zone(None)

    user = frappe.db.get_value(
        "Employee",
        employee,
        "user_id",
        cache=True,
    )

    user_time_zone = (
        frappe.db.get_value(
            "User",
            user,
            "time_zone",
            cache=True,
        )
        if user
        else None
    )

    return _normalise_time_zone(user_time_zone)


def _date_in_time_zone(system_datetime, time_zone_name: str | None) -> date:
    """Return the date of a system-timezone datetime in another timezone."""

    value = get_datetime(system_datetime)
    if not value:
        return getdate(system_datetime)

    system_zone = timezone(_normalise_time_zone(get_system_timezone()))
    target_zone = timezone(_normalise_time_zone(time_zone_name))

    if value.tzinfo is None:
        value = system_zone.localize(value)
    else:
        value = value.astimezone(system_zone)

    return value.astimezone(target_zone).date()


def _context_field_permissions() -> dict[str, bool]:
    """Return which work-context fields the current user may select."""

    project_allowed = has_doctype_permission("Project")

    return {
        "project": project_allowed,
        # A Task is never selectable without a Project, even when the user has
        # standalone Task permission.
        "task": bool(project_allowed and has_doctype_permission("Task")),
        "ticket": has_doctype_permission(TICKET_DOCTYPE),
    }


def _has_context_document_permission(doctype: str, name: str) -> bool:
    return has_document_permission(doctype, name)


def _check_context_document_permission(doctype: str, name: str) -> None:
    if not _has_context_document_permission(doctype, name):
        frappe.throw(
            _("You do not have permission to select {0} {1}.").format(
                doctype,
                frappe.bold(name),
            ),
            frappe.PermissionError,
        )


def _hide_disallowed_context_fields(
    rows: list[dict[str, Any]] | None,
    permissions: dict[str, bool],
) -> list[dict[str, Any]]:
    """Do not expose context values for DocTypes the user cannot read."""

    sanitised_rows: list[dict[str, Any]] = []

    for row in rows or []:
        sanitised = frappe._dict(row)
        context_restricted = bool(sanitised.get("context_restricted"))

        for fieldname in ("project", "task", "ticket"):
            if not permissions.get(fieldname):
                if sanitised.get(fieldname):
                    context_restricted = True
                sanitised[fieldname] = None

        sanitised["context_restricted"] = context_restricted

        sanitised_rows.append(sanitised)

    return sanitised_rows


def _add_context_display_names(
    rows: list[dict[str, Any]] | None,
    permissions: dict[str, bool],
) -> list[dict[str, Any]]:
    """Attach readable labels after record-level permission checks."""

    prepared = [frappe._dict(row) for row in (rows or [])]

    project_names: dict[str, str] = {}
    task_names: dict[str, str] = {}
    visible_ticket_ids: set[str] = set()
    project_ids = {row.project for row in prepared if row.get("project")}
    task_ids = {row.task for row in prepared if row.get("task")}
    ticket_ids = {row.ticket for row in prepared if row.get("ticket")}

    if permissions.get("project"):
        for project_id in project_ids:
            if not _has_context_document_permission("Project", project_id):
                continue
            project = frappe.get_doc("Project", project_id)
            project_names[project_id] = project.project_name or project_id

    if permissions.get("task"):
        for task_id in task_ids:
            if not _has_context_document_permission("Task", task_id):
                continue
            task = frappe.get_doc("Task", task_id)
            task_names[task_id] = task.subject or task_id

    if permissions.get("ticket"):
        visible_ticket_ids = {
            ticket_id
            for ticket_id in ticket_ids
            if _has_context_document_permission(TICKET_DOCTYPE, ticket_id)
        }

    for row in prepared:
        context_restricted = bool(row.get("context_restricted"))

        if row.get("project") and row.project not in project_names:
            context_restricted = True
            row["project"] = None

        if row.get("task") and row.task not in task_names:
            context_restricted = True
            row["task"] = None

        if row.get("ticket") and row.ticket not in visible_ticket_ids:
            context_restricted = True
            row["ticket"] = None

        row["context_restricted"] = context_restricted
        row["project_name"] = (
            project_names.get(row.project, row.project)
            if permissions.get("project") and row.get("project")
            else None
        )
        row["task_name"] = (
            task_names.get(row.task, row.task)
            if permissions.get("task") and row.get("task")
            else None
        )

    return prepared


def _validate_work_context(
    project: str | None,
    task: str | None,
    ticket: str | None,
) -> tuple[str | None, str | None, str | None]:
    project = project or None
    task = task or None
    ticket = ticket or None

    if not any((project, task, ticket)):
        frappe.throw(
            _(
                "Select at least one Project, Task, or Ticket "
                "before starting a session."
            )
        )

    if project:
        if not frappe.db.exists("Project", project):
            frappe.throw(
                _("Project {0} does not exist.").format(frappe.bold(project))
            )
        _check_context_document_permission("Project", project)

    if task:
        if not frappe.db.exists("Task", task):
            frappe.throw(
                _("Task {0} does not exist.").format(frappe.bold(task))
            )

        _check_context_document_permission("Task", task)
        validate_task_project(project, task)

    if ticket:
        if not doctype_exists(TICKET_DOCTYPE):
            frappe.throw(
                _(
                    "Helpdesk is not installed, so Ticket cannot be selected. "
                    "Choose a Project instead."
                )
            )

        if not frappe.db.exists(TICKET_DOCTYPE, ticket):
            frappe.throw(
                _("{0} {1} does not exist.").format(
                    TICKET_DOCTYPE,
                    frappe.bold(ticket),
                )
            )
        _check_context_document_permission(TICKET_DOCTYPE, ticket)

    return project, task, ticket


def _get_tracker_for_read(tracker_name: str):
    if not tracker_name:
        frappe.throw(_("Time Tracker is required."))

    tracker = frappe.get_doc(TRACKER_DOCTYPE, tracker_name)
    tracker.check_permission("read")

    if not can_read_employee(tracker.employee, frappe.session.user):
        frappe.throw(
            _("You are not permitted to view this Time Tracker."),
            frappe.PermissionError,
        )

    return tracker


def _user_owns_tracker(tracker, user: str | None = None) -> bool:
    user = user or frappe.session.user
    return tracker.employee in set(
        get_employees_for_user(user, active_only=False)
    )


def _user_can_manage_browser_widget(tracker, user: str | None = None) -> bool:
    """Allow only the tracker owner or a System Manager to change opt-in."""

    user = user or frappe.session.user
    return is_system_manager(user) or _user_owns_tracker(tracker, user)


def _browser_widget_schema_ready() -> bool:
    """Return whether the opt-in preference column has been migrated."""

    return bool(
        frappe.db.table_exists(TRACKER_DOCTYPE)
        and frappe.db.has_column(TRACKER_DOCTYPE, BROWSER_WIDGET_FIELD)
    )


def _browser_widget_enabled(tracker) -> bool:
    if not _browser_widget_schema_ready():
        return False
    return bool(cint(tracker.get(BROWSER_WIDGET_FIELD)))


def _user_can_control_tracker(tracker) -> bool:
    """Allow the owner or a System Manager to run an active Employee's timer."""

    employee_status = frappe.db.get_value(
        "Employee",
        tracker.employee,
        "status",
    )

    # Historical/inactive employees remain viewable but their timers cannot run.
    if employee_status != "Active":
        return False

    user = frappe.session.user
    return is_system_manager(user) or _user_owns_tracker(tracker, user)


def _salary_slip_permissions() -> dict[str, bool]:
    can_read = has_doctype_permission("Salary Slip", ("read",))

    return {
        "read": can_read,
        "print": bool(
            can_read
            and has_doctype_permission("Salary Slip", ("print",))
        ),
    }


def _get_salary_slips(
    tracker,
    permissions: dict[str, bool],
) -> list[dict[str, Any]]:
    """Return every draft or submitted Salary Slip readable for the Employee."""

    if not permissions.get("read"):
        return []

    fields = [
        "name",
        "start_date",
        "end_date",
        "posting_date",
        "status",
        "docstatus",
        "net_pay",
        "currency",
        "payroll_entry",
        "total_working_hours",
    ]

    for fieldname in (
        "custom_time_tracker_hours",
        "custom_time_tracker_log_count",
        "custom_time_tracking_source",
        "custom_time_tracker",
    ):
        if frappe.db.has_column("Salary Slip", fieldname):
            fields.append(fieldname)

    rows = frappe.get_list(
        "Salary Slip",
        filters={
            "employee": tracker.employee,
            "docstatus": ["in", [0, 1]],
        },
        fields=fields,
        order_by="start_date desc, creation desc",
        limit_page_length=0,
    )

    # ``get_list`` already applies Salary Slip permission query conditions.
    # Keep action buttons record-aware as well, because User Permissions may
    # allow one Salary Slip while denying another.
    for row in rows:
        row["can_view"] = has_document_permission(
            "Salary Slip", row.name, ("read",)
        )
        row["can_print"] = bool(
            permissions.get("print")
            and has_document_permission(
                "Salary Slip", row.name, ("print",)
            )
        )

    return [row for row in rows if row.get("can_view")]


@frappe.whitelist(methods=["POST"])
def get_work_context_permissions() -> dict[str, Any]:
    """Return optional context availability for form and timer controls."""

    return {
        "permissions": _context_field_permissions(),
        "ticket_doctype": (
            TICKET_DOCTYPE if doctype_exists(TICKET_DOCTYPE) else ""
        ),
    }


@frappe.whitelist(methods=["POST"])
def get_my_employee() -> str | None:
    """Return the Employee linked to the current User."""

    return get_employee_for_user(frappe.session.user)


@frappe.whitelist(methods=["POST"])
def get_timer_widget_state() -> dict[str, Any]:
    """Return the current User's opt-in floating-timer state.

    This endpoint never provisions a Time Tracker. The logged-in Employee must
    already own a manually-created tracker and must enable the Browser Widget
    from that tracker before the compact timer is exposed on Desk routes.
    """

    user = frappe.session.user
    if user == "Guest":
        return {
            "available": False,
            "widget_enabled": False,
            "reason": _("Sign in to use Time Tracker."),
        }

    employee = get_employee_for_user(user, active_only=False)
    if not employee:
        return {
            "available": False,
            "widget_enabled": False,
            "reason": _("No Employee is linked to this User."),
        }

    tracker_name = frappe.db.get_value(
        TRACKER_DOCTYPE,
        {"employee": employee},
        "name",
    )

    if not tracker_name:
        return {
            "available": False,
            "widget_enabled": False,
            "reason": _(
                "No Time Tracker exists for this Employee. Create it manually first."
            ),
        }

    if not _browser_widget_schema_ready():
        return {
            "available": False,
            "widget_enabled": False,
            "reason": _(
                "The Browser Widget option is not installed. Run bench migrate for Time Tracker."
            ),
        }

    tracker = frappe.get_doc(TRACKER_DOCTYPE, tracker_name)
    if not frappe.has_permission(
        doctype=TRACKER_DOCTYPE,
        ptype="read",
        doc=tracker,
        user=user,
    ):
        return {
            "available": False,
            "widget_enabled": False,
            "reason": _("Time Tracker read permission is required."),
        }

    if not can_read_employee(tracker.employee, user):
        return {
            "available": False,
            "widget_enabled": False,
            "reason": _("You are not permitted to view this Time Tracker."),
        }

    if not _browser_widget_enabled(tracker):
        return {
            "available": False,
            "widget_enabled": False,
            "tracker": tracker.name,
            "reason": _(
                "Browser Widget is disabled on this Time Tracker."
            ),
        }

    employee_values = frappe.db.get_value(
        "Employee",
        employee,
        ["employee_name", "status"],
        as_dict=True,
    ) or frappe._dict()
    permissions = _context_field_permissions()
    current_time = now_datetime()
    running_rows = frappe.get_all(
        LOG_DOCTYPE,
        filters={
            "time_tracker": tracker.name,
            "status": "Running",
        },
        fields=[
            "name",
            "start_time",
            "project",
            "task",
            "ticket",
            "description",
        ],
        order_by="creation desc",
        limit_page_length=2,
    )

    if len(running_rows) > 1:
        return {
            "available": True,
            "widget_enabled": True,
            "tracker": tracker.name,
            "employee": employee,
            "employee_name": employee_values.employee_name or employee,
            "employee_status": employee_values.status or tracker.status,
            "can_control": False,
            "error": _(
                "More than one running timer exists. Contact an administrator."
            ),
            "running": None,
            "context_permissions": permissions,
            "ticket_doctype": (
                TICKET_DOCTYPE if doctype_exists(TICKET_DOCTYPE) else ""
            ),
        }

    running = None
    if running_rows:
        prepared = _add_context_display_names(
            _hide_disallowed_context_fields(running_rows, permissions),
            permissions,
        )[0]
        running = {
            "name": prepared.name,
            "start_time": prepared.start_time,
            "elapsed_seconds": max(
                0,
                int(time_diff_in_seconds(current_time, prepared.start_time)),
            ),
            "project": prepared.get("project"),
            "project_name": prepared.get("project_name"),
            "task": prepared.get("task"),
            "task_name": prepared.get("task_name"),
            "ticket": prepared.get("ticket"),
            "context_restricted": bool(prepared.get("context_restricted")),
        }

    return {
        "available": True,
        "widget_enabled": True,
        "tracker": tracker.name,
        "employee": employee,
        "employee_name": employee_values.employee_name or employee,
        "employee_status": employee_values.status or tracker.status,
        "can_control": _user_can_control_tracker(tracker),
        "running": running,
        "context_permissions": permissions,
        "ticket_doctype": (
            TICKET_DOCTYPE if doctype_exists(TICKET_DOCTYPE) else ""
        ),
        "server_time": current_time,
    }


@frappe.whitelist(methods=["POST"])
def set_browser_widget_enabled(
    tracker: str,
    enabled: int | str | bool = 0,
) -> dict[str, Any]:
    """Persist the owner's explicit opt-in for the floating Desk widget."""

    if not _browser_widget_schema_ready():
        frappe.throw(
            _(
                "The Browser Widget option is not installed. Run bench migrate "
                "for Time Tracker and try again."
            ),
            title=_("Migration Required"),
        )

    tracker_doc = _get_tracker_for_read(tracker)

    if not _user_can_manage_browser_widget(tracker_doc):
        frappe.throw(
            _(
                "Only the Employee who owns this Time Tracker or a System "
                "Manager can change the Browser Widget preference."
            ),
            frappe.PermissionError,
        )

    enabled_value = 1 if cint(enabled) else 0
    current_value = 1 if _browser_widget_enabled(tracker_doc) else 0

    if current_value != enabled_value:
        frappe.db.set_value(
            TRACKER_DOCTYPE,
            tracker_doc.name,
            BROWSER_WIDGET_FIELD,
            enabled_value,
            update_modified=True,
        )
        tracker_doc.set(BROWSER_WIDGET_FIELD, enabled_value)

    _publish_timer_update(tracker_doc)

    return {
        "tracker": tracker_doc.name,
        "enabled": bool(enabled_value),
        "message": (
            _("Browser Widget enabled.")
            if enabled_value
            else _("Browser Widget disabled. Your running timer, if any, was not stopped.")
        ),
    }


@frappe.whitelist(methods=["POST"])
def toggle_timer(
    tracker: str,
    action: str,
    project: str | None = None,
    task: str | None = None,
    ticket: str | None = None,
) -> dict[str, Any]:
    """Start, stop, or atomically switch the current user's timer."""

    action = (action or "").strip().title()

    if action not in {"Start", "Stop", "Switch"}:
        frappe.throw(_("Action must be Start, Stop, or Switch."))

    tracker_doc = _get_tracker_for_read(tracker)

    if not _user_can_control_tracker(tracker_doc):
        frappe.throw(
            _("Only the tracker owner, Administrator, or a System Manager can control this Time Tracker."),
            frappe.PermissionError,
        )

    # Lock the tracker row so multiple tabs cannot create competing
    # running logs or race while switching work context.
    frappe.db.sql(
        """
        SELECT name
        FROM `tabTime Tracker`
        WHERE name = %s
        FOR UPDATE
        """,
        (tracker_doc.name,),
    )

    running_logs = frappe.get_all(
        LOG_DOCTYPE,
        filters={
            "time_tracker": tracker_doc.name,
            "status": "Running",
        },
        fields=["name"],
        order_by="creation desc",
        limit_page_length=2,
    )

    if len(running_logs) > 1:
        frappe.throw(
            _(
                "More than one running timer exists for this employee. "
                "Contact an administrator."
            )
        )

    current_time = now_datetime()

    if action in {"Start", "Switch"}:
        log_date = _date_in_time_zone(
            current_time,
            _get_employee_time_zone(tracker_doc.employee),
        )

        if action == "Start" and running_logs:
            frappe.throw(
                _("A timer is already running. Stop it before starting another.")
            )

        # Validate the new context before changing the active timer. A failed
        # Project/Task/Ticket validation therefore leaves the current session
        # untouched.
        project, task, ticket = _validate_work_context(
            project,
            task,
            ticket,
        )

        previous_log_name = None

        if action == "Switch" and running_logs:
            previous_log = frappe.get_doc(
                LOG_DOCTYPE,
                running_logs[0].name,
            )
            elapsed_seconds = max(
                0.0,
                time_diff_in_seconds(
                    current_time,
                    previous_log.start_time,
                ),
            )

            previous_log.end_time = current_time
            previous_log.hours = flt(elapsed_seconds / 3600.0, 6)
            previous_log.status = "Stopped"
            previous_log.ended_by = frappe.session.user
            with time_tracker_log_write():
                previous_log.save(ignore_permissions=True)
            previous_log_name = previous_log.name

        log = frappe.get_doc(
            {
                "doctype": LOG_DOCTYPE,
                "time_tracker": tracker_doc.name,
                "employee": tracker_doc.employee,
                "log_date": log_date,
                "status": "Running",
                "start_time": current_time,
                "end_time": None,
                "hours": 0,
                "project": project,
                "task": task,
                "ticket": ticket,
                "started_by": frappe.session.user,
            }
        )
        with time_tracker_log_write():
            log.insert(ignore_permissions=True)

        switched = bool(previous_log_name)

        _publish_timer_update(tracker_doc)

        return {
            "message": _("Session switched.") if switched else _("Timer started."),
            "status": "Running",
            "log_name": log.name,
            "previous_log_name": previous_log_name,
        }

    if not running_logs:
        frappe.throw(_("No running timer was found."))

    log = frappe.get_doc(
        LOG_DOCTYPE,
        running_logs[0].name,
    )

    elapsed_seconds = max(
        0.0,
        time_diff_in_seconds(
            current_time,
            log.start_time,
        ),
    )

    log.end_time = current_time
    log.hours = flt(elapsed_seconds / 3600.0, 6)
    log.status = "Stopped"
    log.ended_by = frappe.session.user
    with time_tracker_log_write():
        log.save(ignore_permissions=True)

    _publish_timer_update(tracker_doc)

    return {
        "message": _("Timer stopped."),
        "status": "Stopped",
        "log_name": log.name,
        "hours": log.hours,
    }


def _publish_timer_update(tracker) -> None:
    """Synchronise the compact widget across the Employee's open Desk tabs."""

    users = {frappe.session.user}
    employee_user = frappe.db.get_value(
        "Employee",
        tracker.employee,
        "user_id",
        cache=True,
    )
    if employee_user:
        users.add(employee_user)

    for user in users:
        if not user or user == "Guest":
            continue
        frappe.publish_realtime(
            "time_tracker_timer_updated",
            {"tracker": tracker.name},
            user=user,
            after_commit=True,
        )



def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_recent_log_date(
    value: str | date | datetime | None,
    label: str,
) -> date | None:
    if not value:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        frappe.throw(
            _("{0} must be a valid date in YYYY-MM-DD format.").format(label)
        )


def _parse_recent_log_date_range(
    from_date: str | date | datetime | None = None,
    to_date: str | date | datetime | None = None,
    legacy_date: str | date | datetime | None = None,
) -> tuple[date | None, date | None]:
    """Parse and validate an inclusive recent-session date range."""

    # Keep cached clients that still send the former single-date argument working.
    if legacy_date and not from_date and not to_date:
        from_date = legacy_date
        to_date = legacy_date

    selected_from_date = _parse_recent_log_date(from_date, _("From Date"))
    selected_to_date = _parse_recent_log_date(to_date, _("To Date"))

    if (
        selected_from_date
        and selected_to_date
        and selected_from_date > selected_to_date
    ):
        frappe.throw(_("From Date cannot be after To Date."))

    return selected_from_date, selected_to_date


def _clamp_past_offset(value, minimum):
    """Return a past-only offset constrained to a safe history window."""

    return max(min(_safe_int(value), 0), minimum)


def _month_shift(source_date, offset):
    """
    Move a date backward or forward by a number of months.

    Example:
        July 2026 + -1 = June 2026
    """

    month_index = (
        source_date.year * 12
        + source_date.month
        - 1
        + offset
    )

    year = month_index // 12
    month = month_index % 12 + 1

    return date(year, month, 1)


def _date_range_for_offsets(
    local_today,
    day_offset=0,
    week_offset=0,
    month_offset=0,
):
    target_day = local_today + timedelta(
        days=day_offset
    )

    current_week_start = (
        local_today
        - timedelta(
            days=local_today.weekday()
        )
    )

    week_start = (
        current_week_start
        + timedelta(
            weeks=week_offset
        )
    )

    week_end = week_start + timedelta(days=6)

    month_start = _month_shift(
        local_today,
        month_offset,
    )

    month_end = date(
        month_start.year,
        month_start.month,
        calendar.monthrange(
            month_start.year,
            month_start.month,
        )[1],
    )

    return {
        "target_day": target_day,
        "week_start": week_start,
        "week_end": week_end,
        "month_start": month_start,
        "month_end": month_end,
    }


def _get_stopped_hours(
    employee,
    start_date,
    end_date,
):
    result = frappe.db.sql(
        """
        SELECT
            COALESCE(SUM(hours), 0) AS total_hours
        FROM `tabTracker Log`
        WHERE employee = %s
          AND status = 'Stopped'
          AND log_date BETWEEN %s AND %s
        """,
        (
            employee,
            start_date,
            end_date,
        ),
        as_dict=True,
    )

    if not result:
        return 0.0

    return flt(
        result[0].total_hours,
        6,
    )


def _running_hours_for_period(
    running_log,
    running_hours,
    start_date,
    end_date,
):
    """
    Include the open timer only when its log_date belongs
    to the requested period.
    """

    if not running_log:
        return 0.0

    if not running_log.get("log_date"):
        return 0.0

    running_date = running_log.log_date

    if isinstance(running_date, datetime):
        running_date = running_date.date()

    if isinstance(running_date, str):
        running_date = get_datetime(
            running_date
        ).date()

    if start_date <= running_date <= end_date:
        return running_hours

    return 0.0


def _percentage(hours, limit):
    if not limit or limit <= 0:
        return 0.0

    return flt(
        min(
            max(
                hours / limit * 100,
                0,
            ),
            100,
        ),
        2,
    )


def _monthly_limit(
    weekly_limit,
    month_start,
    month_end,
):
    """
    Convert the weekly limit to a proportional monthly limit.

    Example:
        40 weekly hours × days in month ÷ 7
    """

    days_in_month = (
        month_end - month_start
    ).days + 1

    return flt(
        weekly_limit
        * days_in_month
        / 7,
        3,
    )


def _get_heatmap_data(
    employee,
    start_date,
    end_date,
):
    rows = frappe.db.sql(
        """
        SELECT
            log_date,
            COALESCE(SUM(hours), 0) AS total_hours
        FROM `tabTracker Log`
        WHERE employee = %s
          AND status = 'Stopped'
          AND log_date BETWEEN %s AND %s
        GROUP BY log_date
        ORDER BY log_date ASC
        """,
        (
            employee,
            start_date,
            end_date,
        ),
        as_dict=True,
    )

    heatmap = {}

    for row in rows:
        if not row.log_date:
            continue

        date_value = row.log_date

        if isinstance(date_value, datetime):
            date_value = date_value.date()

        heatmap[str(date_value)] = flt(
            row.total_hours,
            6,
        )

    return heatmap


def _get_recent_log_page(
    tracker_name: str,
    start: int = 0,
    page_length: int = RECENT_LOG_PAGE_LENGTH,
    from_date: date | None = None,
    to_date: date | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    start = max(_safe_int(start), 0)
    page_length = min(
        max(_safe_int(page_length, RECENT_LOG_PAGE_LENGTH), 1),
        MAX_RECENT_LOG_PAGE_LENGTH,
    )

    filters: dict[str, Any] = {
        "time_tracker": tracker_name,
    }

    if from_date and to_date:
        filters["log_date"] = ["between", [from_date, to_date]]
    elif from_date:
        filters["log_date"] = [">=", from_date]
    elif to_date:
        filters["log_date"] = ["<=", to_date]

    rows = frappe.get_all(
        LOG_DOCTYPE,
        filters=filters,
        fields=RECENT_LOG_FIELDS,
        order_by="creation desc",
        limit_start=start,
        limit_page_length=page_length + 1,
    )

    return rows[:page_length], len(rows) > page_length


@frappe.whitelist(methods=["POST"])
def get_recent_logs(
    tracker: str,
    start: int = 0,
    page_length: int = RECENT_LOG_PAGE_LENGTH,
    from_date: str | None = None,
    to_date: str | None = None,
    log_date: str | None = None,
) -> dict[str, Any]:
    """Return the next in-dashboard page of Tracker Logs for a date range."""

    tracker_doc = _get_tracker_for_read(tracker)
    context_permissions = _context_field_permissions()
    selected_from_date, selected_to_date = _parse_recent_log_date_range(
        from_date=from_date,
        to_date=to_date,
        legacy_date=log_date,
    )
    logs, has_more = _get_recent_log_page(
        tracker_doc.name,
        start=start,
        page_length=page_length,
        from_date=selected_from_date,
        to_date=selected_to_date,
    )

    logs = _hide_disallowed_context_fields(logs, context_permissions)
    logs = _add_context_display_names(logs, context_permissions)

    return {
        "logs": logs,
        "has_more": has_more,
        "from_date": str(selected_from_date) if selected_from_date else "",
        "to_date": str(selected_to_date) if selected_to_date else "",
        # Retain the old response key when the range represents one day.
        "log_date": (
            str(selected_from_date)
            if selected_from_date and selected_from_date == selected_to_date
            else ""
        ),
    }


@frappe.whitelist(methods=["POST"])
def get_dashboard_data(
    tracker,
    day_offset=0,
    week_offset=0,
    month_offset=0,
    recent_log_from_date=None,
    recent_log_to_date=None,
    recent_log_date=None,
):
    tracker_doc = _get_tracker_for_read(
        tracker
    )

    employee_doc = frappe.get_doc(
        "Employee",
        tracker_doc.employee,
    )

    local_today = getdate(now_datetime())
    context_permissions = _context_field_permissions()
    (
        selected_recent_log_from_date,
        selected_recent_log_to_date,
    ) = _parse_recent_log_date_range(
        from_date=recent_log_from_date,
        to_date=recent_log_to_date,
        legacy_date=recent_log_date,
    )

    day_offset = _clamp_past_offset(day_offset, -HISTORY_DAYS)
    week_offset = _clamp_past_offset(week_offset, -(HISTORY_DAYS // 7))
    month_offset = _clamp_past_offset(month_offset, -24)

    periods = _date_range_for_offsets(
        local_today=local_today,
        day_offset=day_offset,
        week_offset=week_offset,
        month_offset=month_offset,
    )

    recent_logs, recent_logs_has_more = _get_recent_log_page(
        tracker_doc.name,
        page_length=RECENT_LOG_PAGE_LENGTH,
    )
    recent_logs = _hide_disallowed_context_fields(
        recent_logs,
        context_permissions,
    )
    recent_logs = _add_context_display_names(
        recent_logs,
        context_permissions,
    )

    recent_activity_logs = recent_logs
    recent_activity_logs_has_more = recent_logs_has_more

    if selected_recent_log_from_date or selected_recent_log_to_date:
        recent_activity_logs, recent_activity_logs_has_more = _get_recent_log_page(
            tracker_doc.name,
            page_length=RECENT_LOG_PAGE_LENGTH,
            from_date=selected_recent_log_from_date,
            to_date=selected_recent_log_to_date,
        )
        recent_activity_logs = _hide_disallowed_context_fields(
            recent_activity_logs,
            context_permissions,
        )
        recent_activity_logs = _add_context_display_names(
            recent_activity_logs,
            context_permissions,
        )

    running_rows = frappe.get_all(
        "Tracker Log",
        filters={
            "time_tracker": tracker_doc.name,
            "status": "Running",
        },
        fields=[
            "name",
            "status",
            "log_date",
            "start_time",
            "project",
            "task",
            "ticket",
            "description",
        ],
        order_by="creation desc",
        limit_page_length=1,
    )

    running_log = (
        running_rows[0]
        if running_rows
        else None
    )

    if running_log:
        running_log = _hide_disallowed_context_fields(
            [running_log],
            context_permissions,
        )[0]
        running_log = _add_context_display_names(
            [running_log],
            context_permissions,
        )[0]

    running_elapsed_seconds = 0
    running_hours = 0.0

    if running_log and running_log.start_time:
        running_elapsed_seconds = max(
            0,
            int(
                time_diff_in_seconds(
                    now_datetime(),
                    running_log.start_time,
                )
            ),
        )

        running_hours = flt(
            running_elapsed_seconds
            / 3600,
            6,
        )

    target_day = periods["target_day"]
    week_start = periods["week_start"]
    week_end = periods["week_end"]
    month_start = periods["month_start"]
    month_end = periods["month_end"]

    daily_stopped_hours = _get_stopped_hours(
        employee=tracker_doc.employee,
        start_date=target_day,
        end_date=target_day,
    )

    weekly_stopped_hours = _get_stopped_hours(
        employee=tracker_doc.employee,
        start_date=week_start,
        end_date=week_end,
    )

    monthly_stopped_hours = _get_stopped_hours(
        employee=tracker_doc.employee,
        start_date=month_start,
        end_date=month_end,
    )

    daily_running_hours = (
        _running_hours_for_period(
            running_log=running_log,
            running_hours=running_hours,
            start_date=target_day,
            end_date=target_day,
        )
    )

    weekly_running_hours = (
        _running_hours_for_period(
            running_log=running_log,
            running_hours=running_hours,
            start_date=week_start,
            end_date=week_end,
        )
    )

    monthly_running_hours = (
        _running_hours_for_period(
            running_log=running_log,
            running_hours=running_hours,
            start_date=month_start,
            end_date=month_end,
        )
    )

    daily_hours = flt(
        daily_stopped_hours
        + daily_running_hours,
        6,
    )

    weekly_hours = flt(
        weekly_stopped_hours
        + weekly_running_hours,
        6,
    )

    monthly_hours = flt(
        monthly_stopped_hours
        + monthly_running_hours,
        6,
    )

    employee_weekly_fields = [
        fieldname
        for fieldname in EMPLOYEE_WEEKLY_LIMIT_FIELDS
        if employee_doc.meta.has_field(fieldname)
    ]
    weekly_settings = resolve_employee_weekly_hours_limit(
        employee_doc,
        available_fields=employee_weekly_fields,
    )
    weekly_limit = flt(weekly_settings.weekly_limit, 3)
    weekly_exceeded_hours = flt(
        max(0, weekly_hours - weekly_limit) if weekly_limit > 0 else 0,
        6,
    )

    monthly_limit = _monthly_limit(
        weekly_limit=weekly_limit,
        month_start=month_start,
        month_end=month_end,
    )

    heatmap_start = min(
        add_days(
            local_today,
            -HISTORY_DAYS,
        ),
        month_start,
    )

    heatmap = _get_heatmap_data(
        employee=tracker_doc.employee,
        start_date=heatmap_start,
        end_date=local_today,
    )

    time_tracker_settings = get_time_tracker_ui_settings(employee_doc.company)
    show_salary_slips = bool(
        time_tracker_settings.show_salary_slip_on_time_tracker
    )
    salary_slip_permissions = (
        _salary_slip_permissions()
        if show_salary_slips
        else {"read": False, "print": False}
    )
    salary_slips = (
        _get_salary_slips(tracker_doc, salary_slip_permissions)
        if show_salary_slips
        else []
    )
    browser_widget_schema_ready = _browser_widget_schema_ready()

    return {
        "profile": {
            "employee": employee_doc.name,
            "employee_name": (
                employee_doc.employee_name
            ),
            "status": employee_doc.status or tracker_doc.status or "",
            "company": (
                employee_doc.company
                or _("Not set")
            ),
            "department": (
                employee_doc.department
                or _("Not set")
            ),
            "designation": (
                employee_doc.designation
                or _("Not set")
            ),
            "date_of_joining": str(
                employee_doc.date_of_joining
                or ""
            ),
            "weekly_limit": weekly_limit,
        },

        "stats": {
            "daily": {
                "date": str(target_day),
                "hours": daily_hours,
                "stopped_hours": (
                    daily_stopped_hours
                ),
                "running_hours": (
                    daily_running_hours
                ),
            },

            "weekly": {
                "start_date": str(
                    week_start
                ),
                "end_date": str(
                    week_end
                ),
                "hours": weekly_hours,
                "stopped_hours": (
                    weekly_stopped_hours
                ),
                "running_hours": (
                    weekly_running_hours
                ),
                "limit": weekly_limit,
                "exceeded_hours": weekly_exceeded_hours,
                "is_exceeded": bool(weekly_exceeded_hours > 0),
                "percentage": _percentage(
                    weekly_hours,
                    weekly_limit,
                ),
            },

            "monthly": {
                "start_date": str(
                    month_start
                ),
                "end_date": str(
                    month_end
                ),
                "hours": monthly_hours,
                "stopped_hours": (
                    monthly_stopped_hours
                ),
                "running_hours": (
                    monthly_running_hours
                ),
                "limit": monthly_limit,
                "percentage": _percentage(
                    monthly_hours,
                    monthly_limit,
                ),
            },
        },

        "offsets": {
            "day": day_offset,
            "week": week_offset,
            "month": month_offset,
        },

        "today": str(local_today),

        "heatmap": {
            "start_date": str(
                heatmap_start
            ),
            "end_date": str(
                local_today
            ),
            "data": heatmap,
        },

        "recent_logs": recent_logs,
        "recent_logs_has_more": recent_logs_has_more,
        "recent_activity_logs": recent_activity_logs,
        "recent_activity_logs_has_more": recent_activity_logs_has_more,
        "recent_log_from_date": (
            str(selected_recent_log_from_date)
            if selected_recent_log_from_date
            else ""
        ),
        "recent_log_to_date": (
            str(selected_recent_log_to_date)
            if selected_recent_log_to_date
            else ""
        ),
        # Retain the former response key for a one-day range.
        "recent_log_date": (
            str(selected_recent_log_from_date)
            if (
                selected_recent_log_from_date
                and selected_recent_log_from_date == selected_recent_log_to_date
            )
            else ""
        ),
        "recent_logs_page_length": RECENT_LOG_PAGE_LENGTH,
        "running_log": running_log,

        "running_elapsed_seconds": (
            running_elapsed_seconds
        ),

        "can_control": (
            _user_can_control_tracker(
                tracker_doc
            )
        ),
        "browser_widget_schema_ready": browser_widget_schema_ready,
        "browser_widget_enabled": bool(
            browser_widget_schema_ready
            and _browser_widget_enabled(tracker_doc)
        ),
        "can_manage_browser_widget": bool(
            _user_can_manage_browser_widget(tracker_doc)
        ),

        "ticket_doctype": (
            TICKET_DOCTYPE
            if doctype_exists(TICKET_DOCTYPE)
            else ""
        ),
        "context_permissions": context_permissions,
        "show_salary_slips": show_salary_slips,
        "salary_slip_print_format": (
            time_tracker_settings.salary_slip_print_format
            or "Time Tracker Salary Slip"
        ),
        "salary_slips": salary_slips,
        "salary_slip_permissions": salary_slip_permissions,
    }
