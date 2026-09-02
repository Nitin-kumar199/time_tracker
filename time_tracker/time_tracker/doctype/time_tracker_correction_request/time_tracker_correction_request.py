from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime, nowdate

from time_tracker.permission_utils import (
    doctype_exists,
    has_document_permission,
)
from time_tracker.permissions import (
    ROLE_MANAGER,
    get_employee_for_user,
    is_hr_manager,
    is_system_manager,
    is_tracker_log_editor,
)
from time_tracker.time_tracker_security import time_tracker_log_write
from time_tracker.work_context import validate_task_project


REQUESTED = "Requested"
APPROVED = "Approved"
REJECTED = "Rejected"
UPDATED = "Updated"  # legacy terminal state from releases before date-level requests
TERMINAL_STATES = {APPROVED, REJECTED, UPDATED}
TICKET_DOCTYPE = "HD Ticket"
TRACKER_REQUEST_LINK_FIELD = "time_tracker_correction_request"
DEFAULT_NEW_LOG_START = time(9, 0, 0)

HARD_IDENTITY_FIELDS = (
    "request_date",
    "requested_by",
    "employee",
    "time_tracker",
)
SERVER_AUDIT_FIELDS = (
    "reviewed_by",
    "reviewed_on",
    "updated_by",
    "updated_on",
    "updated_log_count",
    "created_log_count",
    "updated_tracker_log",
)
ROW_REQUEST_FIELDS = (
    "tracker_log",
    "start_time",
    "hours",
    "project",
    "task",
    "ticket_doctype",
    "ticket",
    "description",
)
ROW_SERVER_FIELDS = (
    "row_type",
    "original_start_time",
    "original_end_time",
    "original_hours",
    "original_project",
    "original_task",
    "original_ticket",
    "original_description",
    "original_salary_slip",
    "source_modified",
    "salary_slip",
    "correction_locked",
    "lock_reason",
    "applied_tracker_log",
    "application_status",
)
TRACKER_LOG_FIELDS = (
    "name",
    "employee",
    "time_tracker",
    "log_date",
    "start_time",
    "end_time",
    "hours",
    "status",
    "project",
    "task",
    "ticket_doctype",
    "ticket",
    "description",
    "salary_slip",
    "modified",
)


class TimeTrackerCorrectionRequest(Document):
    """A date-level correction request containing every stopped log for a day.

    Employees edit the requested values in ``logs``. The source Tracker Logs are
    not changed when the request is approved. After approval, the separate
    ``Update Tracker Log`` workflow action applies changed rows and creates
    Tracker Logs for every new row in one transaction.
    """

    def before_insert(self):
        self._set_new_request_defaults()
        self._set_employee_context(use_current_user=True)
        self._migrate_legacy_single_log_row()
        self._normalise_rows()

    def validate(self):
        previous = self.get_doc_before_save()

        if self.is_new():
            self._set_new_request_defaults()
            self._clear_server_audit_fields()
        elif previous:
            self._restore_server_audit_fields(previous)
            self._restore_row_server_fields(previous)

        self._set_employee_context(use_current_user=self.is_new())
        self._migrate_legacy_single_log_row(previous)
        self._normalise_rows()
        self._validate_immutable_fields(previous)
        self._validate_manager_remarks(previous)
        self._validate_requester(previous)
        self._validate_correction_date()
        self._validate_rows(previous)
        self._validate_transition(previous)
        self._validate_duplicate_open_request()
        self._set_request_summary()

    def before_save(self):
        previous = self.get_doc_before_save()
        previous_state = previous.workflow_state if previous else None
        current_state = self.workflow_state or REQUESTED

        if previous_state == REQUESTED and current_state in {APPROVED, REJECTED}:
            self.reviewed_by = frappe.session.user
            self.reviewed_on = now_datetime()

        apply_tracker_update = (
            previous_state == APPROVED
            and current_state == UPDATED
            and not previous.get("updated_on")
        )
        if apply_tracker_update:
            self._apply_approved_rows()
            self.updated_by = frappe.session.user
            self.updated_on = now_datetime()

    @frappe.whitelist()
    def load_employee_context(self):
        """Fill the signed-in employee and permanent Time Tracker on a new form."""

        if not self.is_new():
            frappe.throw(_("Employee details are fixed after the request is created."))

        self.check_permission("create")
        self._set_new_request_defaults()
        self._set_employee_context(use_current_user=True)
        return self._employee_context_payload()

    @frappe.whitelist()
    def load_tracker_logs(self):
        """Load all stopped Tracker Logs for the selected employee work date."""

        if not self.is_new() and (self.workflow_state or REQUESTED) != REQUESTED:
            frappe.throw(_("Tracker Logs can only be reloaded while the request is Requested."))

        self.check_permission("create" if self.is_new() else "write")
        self._set_new_request_defaults(only_missing=True)
        self._set_employee_context(use_current_user=self.is_new())
        self._validate_requester(self.get_doc_before_save())
        self._validate_correction_date()

        stopped_logs, running_logs = self._get_logs_for_date()
        self.set("logs", [])
        for log in stopped_logs:
            row = self.append("logs", {})
            self._set_row_from_log(row, log)

        self._set_request_summary()
        payload = self._employee_context_payload()
        payload.update(
            {
                "logs": [row.as_dict(no_nulls=False) for row in self.logs],
                "running_logs": [log.name for log in running_logs],
                "existing_log_count": self.existing_log_count,
                "new_log_count": self.new_log_count,
                "requested_total_hours": self.requested_total_hours,
            }
        )
        return payload

    def _set_new_request_defaults(self, *, only_missing: bool = False) -> None:
        values = {
            "request_date": nowdate(),
            "requested_by": frappe.session.user,
            "workflow_state": REQUESTED,
        }
        for fieldname, value in values.items():
            if not only_missing or not self.get(fieldname):
                self.set(fieldname, value)

        if not only_missing:
            self.manager_remarks = None

    def _set_employee_context(self, *, use_current_user: bool) -> None:
        if use_current_user:
            employee = get_employee_for_user(frappe.session.user, active_only=True)
            if not employee:
                frappe.throw(
                    _(
                        "The signed-in User must be linked to an active Employee "
                        "before creating a correction request."
                    ),
                    frappe.PermissionError,
                )
            self.employee = employee
        elif not self.employee:
            frappe.throw(_("Employee is required."))

        employee = frappe.db.get_value(
            "Employee",
            self.employee,
            [
                "name",
                "employee_name",
                "user_id",
                "company",
                "department",
                "designation",
                "status",
            ],
            as_dict=True,
        )
        if not employee:
            frappe.throw(
                _("Employee {0} does not exist.").format(frappe.bold(self.employee))
            )
        if employee.status != "Active" and (self.workflow_state or REQUESTED) == REQUESTED:
            frappe.throw(_("Only an active Employee can request a time correction."))

        tracker = frappe.db.get_value(
            "Time Tracker",
            {"employee": employee.name},
            "name",
        )
        if not tracker and use_current_user:
            # Employee onboarding is idempotent. Provisioning here also repairs
            # employees created before the automation fix.
            from time_tracker.events.employee import ensure_time_tracker_for_employee

            tracker = ensure_time_tracker_for_employee(employee)

        if not tracker:
            frappe.throw(
                _(
                    "Employee {0} does not have a Time Tracker. Enable employee "
                    "automation or create the permanent tracker first."
                ).format(frappe.bold(employee.name))
            )

        if self.time_tracker and self.time_tracker != tracker:
            frappe.throw(_("The linked Time Tracker does not belong to this Employee."))

        self.employee = employee.name
        self.employee_name = employee.employee_name or employee.name
        self.employee_user = employee.user_id
        self.company = employee.company
        self.department = employee.department
        self.designation = employee.designation
        self.time_tracker = tracker

    def _employee_context_payload(self) -> dict[str, Any]:
        return {
            "request_date": self.request_date,
            "requested_by": self.requested_by,
            "workflow_state": self.workflow_state,
            "employee": self.employee,
            "employee_name": self.employee_name,
            "employee_user": self.employee_user,
            "company": self.company,
            "department": self.department,
            "designation": self.designation,
            "time_tracker": self.time_tracker,
        }

    def _get_logs_for_date(self) -> tuple[list[frappe._dict], list[frappe._dict]]:
        rows = frappe.get_all(
            "Tracker Log",
            filters={
                "employee": self.employee,
                "time_tracker": self.time_tracker,
                "log_date": getdate(self.correction_date),
            },
            fields=list(TRACKER_LOG_FIELDS),
            order_by="start_time asc, creation asc",
            limit_page_length=0,
        )
        stopped = [frappe._dict(row) for row in rows if row.status == "Stopped"]
        running = [frappe._dict(row) for row in rows if row.status == "Running"]
        return stopped, running

    def _normalise_rows(self) -> None:
        for row in self.get("logs") or []:
            row.row_type = "Existing Log" if row.tracker_log else "New Log"
            row.hours = flt(row.hours, 6)
            row.project = row.project or None
            row.task = row.task or None
            row.ticket = row.ticket or None
            row.ticket_doctype = (
                TICKET_DOCTYPE
                if row.ticket
                else (row.ticket_doctype or TICKET_DOCTYPE)
            )
            row.description = (row.description or "").strip()

            if not row.tracker_log:
                row.salary_slip = None
                row.correction_locked = 0
                row.lock_reason = None
                for fieldname in (
                    "original_start_time",
                    "original_end_time",
                    "original_hours",
                    "original_project",
                    "original_task",
                    "original_ticket",
                    "original_description",
                    "original_salary_slip",
                    "source_modified",
                ):
                    row.set(fieldname, None)

    def _validate_immutable_fields(self, previous) -> None:
        if not previous:
            return

        changed_identity = [
            fieldname
            for fieldname in HARD_IDENTITY_FIELDS
            if self.has_value_changed(fieldname)
        ]
        if changed_identity:
            frappe.throw(
                _("These request identity fields cannot be changed: {0}.").format(
                    ", ".join(frappe.bold(fieldname) for fieldname in changed_identity)
                )
            )

        if self._is_requester_edit(previous) or getattr(
            self.flags, "time_tracker_legacy_row_materialized", False
        ):
            return

        if self.has_value_changed("correction_date") or self._rows_changed(previous):
            frappe.throw(
                _(
                    "The work date and requested log values cannot be changed during "
                    "manager review or after approval."
                )
            )

    def _validate_manager_remarks(self, previous) -> None:
        if previous and self.has_value_changed("manager_remarks") and not self._is_manager():
            frappe.throw(
                _("Only a reviewing manager can change Manager Remarks."),
                frappe.PermissionError,
            )

        old_state = previous.workflow_state if previous else None
        new_state = self.workflow_state or REQUESTED
        if (
            old_state == REQUESTED
            and new_state == REJECTED
            and not (self.manager_remarks or "").strip()
        ):
            frappe.throw(_("Manager Remarks are required when rejecting a request."))

    def _validate_requester(self, previous) -> None:
        if not self._is_requester_edit(previous):
            return

        if self.requested_by != frappe.session.user:
            frappe.throw(
                _("Only the requester can edit requested log values."),
                frappe.PermissionError,
            )

        own_employee = get_employee_for_user(frappe.session.user, active_only=True)
        if not own_employee or own_employee != self.employee:
            frappe.throw(
                _("You may request a correction only for your own active Employee record."),
                frappe.PermissionError,
            )

    def _validate_correction_date(self) -> None:
        if not self.correction_date:
            frappe.throw(_("Work Date to Correct is required."))
        self.correction_date = getdate(self.correction_date)
        if self.correction_date > getdate(nowdate()):
            frappe.throw(_("Work Date to Correct cannot be in the future."))

    def _validate_rows(self, previous) -> None:
        rows = self.get("logs") or []
        if not rows:
            frappe.throw(
                _(
                    "Add at least one correction row. Select the work date to load "
                    "existing logs, or add a row for missing time."
                )
            )

        tracker_log_names = [row.tracker_log for row in rows if row.tracker_log]
        if len(tracker_log_names) != len(set(tracker_log_names)):
            frappe.throw(_("The same Tracker Log cannot appear more than once."))

        old_state = previous.workflow_state if previous else None
        new_state = self.workflow_state or REQUESTED
        approving = old_state == REQUESTED and new_state == APPROVED
        if (self._is_requester_edit(previous) or approving) and not getattr(
            self.flags, "time_tracker_legacy_row_materialized", False
        ):
            self._validate_loaded_log_set(tracker_log_names)

        actual_logs = {
            row.name: frappe._dict(row)
            for row in frappe.get_all(
                "Tracker Log",
                filters={"name": ["in", tracker_log_names]},
                fields=list(TRACKER_LOG_FIELDS),
                limit_page_length=0,
            )
        } if tracker_log_names else {}

        changed_or_new = False
        total_hours = 0.0
        for row in rows:
            if flt(row.hours) <= 0 or flt(row.hours) > 24:
                frappe.throw(
                    _(
                        "Row {0}: Corrected Hours must be greater than 0 "
                        "and no more than 24."
                    ).format(row.idx)
                )
            total_hours += flt(row.hours)

            if row.tracker_log:
                actual = actual_logs.get(row.tracker_log)
                if not actual:
                    frappe.throw(
                        _("Row {0}: Tracker Log {1} no longer exists.").format(
                            row.idx, frappe.bold(row.tracker_log)
                        )
                    )
                self._validate_existing_log_identity(row, actual)

                if self.is_new() or not row.source_modified:
                    self._set_source_snapshot(row, actual)
                else:
                    self._validate_source_snapshot(row, actual)

                if not row.start_time:
                    row.start_time = _time_part(actual.start_time)

                changed = self._row_has_requested_change(row)
                if actual.salary_slip and changed:
                    frappe.throw(
                        _(
                            "Row {0}: Cancel linked Salary Slip {1} before changing "
                            "this Tracker Log."
                        ).format(row.idx, frappe.bold(actual.salary_slip))
                    )
                changed_or_new = changed_or_new or changed
                self._validate_row_context(row, actual, previous)
            else:
                changed_or_new = True
                if not row.description:
                    row.description = _("Approved time correction for {0}").format(
                        self.correction_date
                    )
                self._validate_row_context(row, None, previous)

        if total_hours > 24.000001:
            frappe.throw(
                _("Requested Total Hours for one work date cannot exceed 24 hours.")
            )

        if not changed_or_new:
            frappe.throw(
                _("Change at least one existing row or add a row for missing time.")
            )

    def _validate_loaded_log_set(self, tracker_log_names: list[str]) -> None:
        expected_logs, _running = self._get_logs_for_date()
        expected = {log.name for log in expected_logs}
        supplied = set(tracker_log_names)
        if expected == supplied:
            return

        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        details = []
        if missing:
            details.append(_("missing: {0}").format(", ".join(missing)))
        if extra:
            details.append(_("not on this date: {0}").format(", ".join(extra)))
        frappe.throw(
            _(
                "The existing-log rows are out of date ({0}). The employee must "
                "reload Tracker Logs for the selected date before approval."
            ).format("; ".join(details) or _("source changed"))
        )

    def _validate_existing_log_identity(self, row, actual: frappe._dict) -> None:
        if actual.employee != self.employee or actual.time_tracker != self.time_tracker:
            frappe.throw(
                _("Row {0}: Tracker Log, Employee, and Time Tracker do not match.").format(row.idx)
            )
        if getdate(actual.log_date) != getdate(self.correction_date):
            frappe.throw(
                _("Row {0}: Tracker Log is not from the selected work date.").format(row.idx)
            )
        if actual.status != "Stopped" or not actual.start_time or not actual.end_time:
            frappe.throw(
                _("Row {0}: Only a stopped Tracker Log can be corrected.").format(row.idx)
            )

    def _validate_row_context(self, row, actual: frappe._dict | None, previous) -> None:
        if not any((row.project, row.task, row.ticket)):
            frappe.throw(
                _("Row {0}: Select at least one Project, Task, or Ticket.").format(row.idx)
            )

        context_changed = actual is None or any(
            (row.get(fieldname) or None) != (actual.get(fieldname) or None)
            for fieldname in ("project", "task", "ticket")
        )
        if context_changed:
            for doctype, value in (("Project", row.project), ("Task", row.task)):
                if value and not frappe.db.exists(doctype, value):
                    frappe.throw(
                        _("Row {0}: {1} {2} does not exist.").format(
                            row.idx, doctype, frappe.bold(value)
                        )
                    )
            validate_task_project(row.project, row.task)

            if row.ticket:
                if not doctype_exists(TICKET_DOCTYPE):
                    frappe.throw(
                        _(
                            "Row {0}: HD Ticket is unavailable because Frappe "
                            "Helpdesk is not installed."
                        ).format(row.idx)
                    )
                if not frappe.db.exists(TICKET_DOCTYPE, row.ticket):
                    frappe.throw(
                        _("Row {0}: HD Ticket {1} does not exist.").format(
                            row.idx, frappe.bold(row.ticket)
                        )
                    )

        if not self._is_requester_edit(previous) or not context_changed:
            return

        for doctype, value in (("Project", row.project), ("Task", row.task)):
            if value and not has_document_permission(doctype, value):
                frappe.throw(
                    _("Row {0}: You do not have permission to select {1} {2}.").format(
                        row.idx, doctype, frappe.bold(value)
                    ),
                    frappe.PermissionError,
                )
        if row.ticket and not has_document_permission(TICKET_DOCTYPE, row.ticket):
            frappe.throw(
                _("Row {0}: You do not have permission to select HD Ticket {1}.").format(
                    row.idx, frappe.bold(row.ticket)
                ),
                frappe.PermissionError,
            )

    def _validate_transition(self, previous) -> None:
        if not previous:
            if (self.workflow_state or REQUESTED) != REQUESTED:
                frappe.throw(_("A new correction request must start in Requested state."))
            return

        old_state = previous.workflow_state or REQUESTED
        new_state = self.workflow_state or REQUESTED
        if old_state == new_state:
            if old_state in TERMINAL_STATES:
                frappe.throw(
                    _(
                        "An approved, rejected, or legacy-updated request "
                        "cannot be changed."
                    )
                )
            if old_state == REQUESTED and not self._can_edit_requested_state():
                frappe.throw(
                    _("Only the requester or a reviewing manager can edit this request."),
                    frappe.PermissionError,
                )
            return

        allowed_transitions = {
            (REQUESTED, APPROVED),
            (REQUESTED, REJECTED),
        }
        if old_state == APPROVED and not previous.get("updated_on"):
            allowed_transitions.add((APPROVED, UPDATED))

        if (old_state, new_state) not in allowed_transitions:
            frappe.throw(
                _("Correction request cannot move from {0} to {1}.").format(
                    frappe.bold(old_state), frappe.bold(new_state)
                )
            )

        if not self._is_manager():
            frappe.throw(
                _("Only a manager can approve, reject, or update Tracker Logs for a correction request."),
                frappe.PermissionError,
            )
        if self.requested_by == frappe.session.user:
            frappe.throw(
                _("You cannot approve or reject your own correction request."),
                frappe.PermissionError,
            )

    def _validate_duplicate_open_request(self) -> None:
        if (
            not self.employee
            or not self.correction_date
            or (self.workflow_state or REQUESTED) != REQUESTED
        ):
            return

        duplicate = frappe.db.exists(
            "Time Tracker Correction Request",
            {
                "employee": self.employee,
                "correction_date": getdate(self.correction_date),
                "workflow_state": REQUESTED,
                "name": ["!=", self.name or ""],
            },
        )
        if duplicate:
            frappe.throw(
                _("Open correction request {0} already exists for this work date.").format(
                    frappe.bold(duplicate)
                )
            )

    def _lock_request_for_application(self) -> None:
        rows = frappe.db.sql(
            """
            SELECT workflow_state, updated_on
            FROM `tabTime Tracker Correction Request`
            WHERE name = %s
            FOR UPDATE
            """,
            (self.name,),
            as_dict=True,
        )
        if not rows:
            frappe.throw(_("Correction Request {0} no longer exists.").format(self.name))

        stored = frappe._dict(rows[0])
        previous = self.get_doc_before_save()
        expected_state = (previous.workflow_state or REQUESTED) if previous else None
        if stored.updated_on or (expected_state and stored.workflow_state != expected_state):
            frappe.throw(
                _(
                    "Correction Request {0} was already reviewed or changed by another "
                    "user. Reload it before continuing."
                ).format(frappe.bold(self.name))
            )

    def _apply_approved_rows(self) -> None:
        self._lock_request_for_application()

        existing_rows = [row for row in self.logs if row.tracker_log]
        new_rows = [row for row in self.logs if not row.tracker_log]
        source_names = [row.tracker_log for row in existing_rows]

        if source_names:
            placeholders = ", ".join(["%s"] * len(source_names))
            frappe.db.sql(
                f"SELECT name FROM `tabTracker Log` WHERE name IN ({placeholders}) FOR UPDATE",
                tuple(source_names),
            )

        updated = 0
        created = 0
        first_applied = None
        latest_end = datetime.combine(getdate(self.correction_date), DEFAULT_NEW_LOG_START)

        with time_tracker_log_write():
            for row in existing_rows:
                log = frappe.get_doc("Tracker Log", row.tracker_log)
                actual = frappe._dict(
                    {
                        fieldname: log.get(fieldname)
                        for fieldname in TRACKER_LOG_FIELDS
                    }
                )
                self._validate_existing_log_identity(row, actual)
                self._validate_source_snapshot(row, actual)

                start_dt = self._row_start_datetime(row, fallback=get_datetime(log.start_time))
                end_dt = start_dt + timedelta(hours=flt(row.hours))
                self._validate_interval(row, start_dt, end_dt)
                latest_end = max(latest_end, end_dt)

                if not self._row_has_requested_change(row):
                    row.applied_tracker_log = log.name
                    row.application_status = "No Change"
                    continue
                if log.salary_slip:
                    frappe.throw(
                        _("Cancel linked Salary Slip {0} before approving this request.").format(
                            frappe.bold(log.salary_slip)
                        )
                    )

                log.log_date = getdate(self.correction_date)
                log.start_time = start_dt
                log.end_time = end_dt
                log.project = row.project or None
                log.task = row.task or None
                log.ticket_doctype = (
                    TICKET_DOCTYPE
                    if row.ticket
                    else (log.ticket_doctype or TICKET_DOCTYPE)
                )
                log.ticket = row.ticket or None
                log.description = (row.description or "").strip()
                log.status = "Stopped"
                log.ended_by = frappe.session.user
                log.set(TRACKER_REQUEST_LINK_FIELD, self.name)
                log.save(ignore_permissions=True)

                row.applied_tracker_log = log.name
                row.application_status = "Updated"
                first_applied = first_applied or log.name
                updated += 1

            for row in new_rows:
                start_dt = self._row_start_datetime(row, fallback=latest_end)
                end_dt = start_dt + timedelta(hours=flt(row.hours))
                self._validate_interval(row, start_dt, end_dt)
                latest_end = max(latest_end, end_dt)

                log = frappe.get_doc(
                    {
                        "doctype": "Tracker Log",
                        "time_tracker": self.time_tracker,
                        "employee": self.employee,
                        "log_date": getdate(self.correction_date),
                        "start_time": start_dt,
                        "end_time": end_dt,
                        "status": "Stopped",
                        "project": row.project or None,
                        "task": row.task or None,
                        "ticket_doctype": TICKET_DOCTYPE,
                        "ticket": row.ticket or None,
                        "description": (row.description or "").strip(),
                        "started_by": self.requested_by,
                        "ended_by": frappe.session.user,
                        TRACKER_REQUEST_LINK_FIELD: self.name,
                    }
                )
                log.insert(ignore_permissions=True)

                row.applied_tracker_log = log.name
                row.application_status = "Created"
                first_applied = first_applied or log.name
                created += 1

        self.updated_log_count = updated
        self.created_log_count = created
        self.updated_tracker_log = first_applied

    def _row_start_datetime(self, row, *, fallback: datetime) -> datetime:
        parsed = _coerce_time(row.start_time)
        if parsed is None:
            return fallback.replace(microsecond=0)
        return datetime.combine(getdate(self.correction_date), parsed)

    def _validate_interval(self, row, start_dt: datetime, end_dt: datetime) -> None:
        day_start = datetime.combine(getdate(self.correction_date), time.min)
        day_end = day_start + timedelta(days=1)
        if (
            start_dt < day_start
            or start_dt >= day_end
            or end_dt <= start_dt
            or end_dt > day_end
        ):
            frappe.throw(
                _(
                    "Row {0}: the corrected interval must start and end within "
                    "the selected work date."
                ).format(row.idx)
            )

    def _set_row_from_log(self, row, log: frappe._dict) -> None:
        row.tracker_log = log.name
        row.row_type = "Existing Log"
        row.start_time = _time_part(log.start_time)
        row.hours = flt(log.hours, 6)
        row.project = log.project
        row.task = log.task
        row.ticket_doctype = log.ticket_doctype or TICKET_DOCTYPE
        row.ticket = log.ticket
        row.description = log.description
        self._set_source_snapshot(row, log)

    def _set_source_snapshot(self, row, actual: frappe._dict) -> None:
        row.row_type = "Existing Log"
        row.original_start_time = _time_part(actual.start_time)
        row.original_end_time = actual.end_time
        row.original_hours = flt(actual.hours, 6)
        row.original_project = actual.project
        row.original_task = actual.task
        row.original_ticket = actual.ticket
        row.original_description = actual.description
        row.original_salary_slip = actual.salary_slip
        row.source_modified = actual.modified
        row.salary_slip = actual.salary_slip
        row.correction_locked = cint(bool(actual.salary_slip))
        row.lock_reason = (
            _("Linked to Salary Slip {0}").format(actual.salary_slip)
            if actual.salary_slip
            else None
        )

    def _validate_source_snapshot(self, row, actual: frappe._dict) -> None:
        expected = {
            "modified": _datetime_key(row.source_modified),
            "start_time": _time_key(row.original_start_time),
            "end_time": _datetime_key(row.original_end_time),
            "hours": round(flt(row.original_hours), 6),
            "project": row.original_project or None,
            "task": row.original_task or None,
            "ticket": row.original_ticket or None,
            "description": (row.original_description or "").strip(),
            "salary_slip": row.original_salary_slip or None,
        }
        current = {
            "modified": _datetime_key(actual.modified),
            "start_time": _time_key(actual.start_time),
            "end_time": _datetime_key(actual.end_time),
            "hours": round(flt(actual.hours), 6),
            "project": actual.project or None,
            "task": actual.task or None,
            "ticket": actual.ticket or None,
            "description": (actual.description or "").strip(),
            "salary_slip": actual.salary_slip or None,
        }
        if expected != current:
            frappe.throw(
                _(
                    "Tracker Log {0} changed after this request loaded it. Reject "
                    "this request or return it to the employee and reload the date."
                ).format(frappe.bold(row.tracker_log))
            )

    def _row_has_requested_change(self, row) -> bool:
        return any(
            (
                _time_key(row.start_time) != _time_key(row.original_start_time),
                abs(flt(row.hours) - flt(row.original_hours)) > 0.000001,
                (row.project or None) != (row.original_project or None),
                (row.task or None) != (row.original_task or None),
                (row.ticket or None) != (row.original_ticket or None),
                (row.description or "").strip() != (row.original_description or "").strip(),
            )
        )

    def _set_request_summary(self) -> None:
        rows = self.get("logs") or []
        self.existing_log_count = sum(1 for row in rows if row.tracker_log)
        self.new_log_count = sum(1 for row in rows if not row.tracker_log)
        self.requested_total_hours = flt(sum(flt(row.hours) for row in rows), 6)

    def _rows_changed(self, previous) -> bool:
        return self._row_payload(self.get("logs") or []) != self._row_payload(
            previous.get("logs") or []
        )

    @staticmethod
    def _row_payload(rows) -> list[tuple[Any, ...]]:
        payload = []
        for row in rows:
            payload.append(
                tuple(
                    _time_key(row.get(fieldname)) if fieldname == "start_time" else (
                        round(flt(row.get(fieldname)), 6)
                        if fieldname == "hours"
                        else (row.get(fieldname) or None)
                    )
                    for fieldname in ROW_REQUEST_FIELDS
                )
            )
        return payload

    def _restore_row_server_fields(self, previous) -> None:
        by_name = {row.name: row for row in previous.get("logs") or [] if row.name}
        by_log = {
            row.tracker_log: row
            for row in previous.get("logs") or []
            if row.tracker_log
        }
        for row in self.get("logs") or []:
            old = by_name.get(row.name) or (
                by_log.get(row.tracker_log) if row.tracker_log else None
            )
            if old:
                for fieldname in ROW_SERVER_FIELDS:
                    row.set(fieldname, old.get(fieldname))
            else:
                for fieldname in ROW_SERVER_FIELDS:
                    row.set(fieldname, None)

    def _clear_server_audit_fields(self) -> None:
        for fieldname in SERVER_AUDIT_FIELDS:
            self.set(fieldname, None)
        for row in self.get("logs") or []:
            row.applied_tracker_log = None
            row.application_status = None

    def _restore_server_audit_fields(self, previous) -> None:
        for fieldname in SERVER_AUDIT_FIELDS:
            self.set(fieldname, previous.get(fieldname))

    def _migrate_legacy_single_log_row(self, previous=None) -> None:
        """Materialise a pre-upgrade one-log request from authoritative values."""

        legacy = None
        if (
            previous
            and not (previous.get("logs") or [])
            and previous.get("tracker_log")
        ):
            legacy = previous
            self.set("logs", [])
            self.flags.time_tracker_legacy_row_materialized = True
        elif not (self.get("logs") or []) and self.get("tracker_log"):
            legacy = self

        if not legacy:
            return

        log = frappe.db.get_value(
            "Tracker Log",
            legacy.tracker_log,
            list(TRACKER_LOG_FIELDS),
            as_dict=True,
        )
        if not log:
            return

        # Date-level requests cannot move a source log to another work date. For
        # old pending requests, preserve the requested duration/context while
        # anchoring the materialised row to the source log's current work date.
        self.correction_date = log.log_date
        row = self.append("logs", {})
        self._set_row_from_log(row, frappe._dict(log))
        row.hours = flt(legacy.get("total_hours") or log.hours, 6)
        row.project = legacy.get("project") or log.project
        row.task = legacy.get("task") or log.task
        row.description = legacy.get("description") or log.description
        if legacy.get("updated_tracker_log"):
            row.applied_tracker_log = legacy.updated_tracker_log
            row.application_status = "Updated"

    def _is_requester_edit(self, previous) -> bool:
        if self.is_new() or not previous:
            return True
        return bool(
            (previous.workflow_state or REQUESTED) == REQUESTED
            and (self.workflow_state or REQUESTED) == REQUESTED
            and self.requested_by == frappe.session.user
        )

    def _can_edit_requested_state(self) -> bool:
        return bool(
            self.requested_by == frappe.session.user
            or self._is_manager()
            or is_system_manager(frappe.session.user)
        )

    @staticmethod
    def _is_manager() -> bool:
        roles = set(frappe.get_roles(frappe.session.user))
        return bool(
            is_system_manager(frappe.session.user)
            or is_hr_manager(frappe.session.user)
            or is_tracker_log_editor(frappe.session.user)
            or ROLE_MANAGER in roles
        )


def _coerce_time(value: Any) -> time | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.time().replace(microsecond=0)
    if isinstance(value, time):
        return value.replace(microsecond=0)
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds()) % 86400
        return time(seconds // 3600, (seconds % 3600) // 60, seconds % 60)

    text = str(value).strip().split(".")[0]
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    frappe.throw(_("Invalid time value: {0}").format(frappe.bold(text)))
    return None


def _time_part(value: Any) -> time | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.time().replace(microsecond=0)
    try:
        return get_datetime(value).time().replace(microsecond=0)
    except Exception:
        return _coerce_time(value)


def _time_key(value: Any) -> str | None:
    parsed = _time_part(value)
    return parsed.strftime("%H:%M:%S") if parsed else None


def _datetime_key(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return get_datetime(value).replace(microsecond=0).isoformat(sep=" ")
