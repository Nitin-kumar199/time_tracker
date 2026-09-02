from __future__ import annotations

from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_datetime, getdate, now_datetime, nowdate

from time_tracker.permissions import (
    ROLE_MANAGER,
    get_employee_for_user,
    is_hr_manager,
    is_system_manager,
    is_tracker_log_editor,
)
from time_tracker.permission_utils import has_document_permission
from time_tracker.time_tracker_security import time_tracker_log_write
from time_tracker.work_context import validate_task_project


REQUESTED = "Requested"
APPROVED = "Approved"
REJECTED = "Rejected"
UPDATED = "Updated"
TERMINAL_STATES = {REJECTED, UPDATED}
IDENTITY_FIELDS = (
    "request_date",
    "requested_by",
    "tracker_log",
    "employee",
    "time_tracker",
)
REQUEST_VALUE_FIELDS = (
    "correction_date",
    "total_hours",
    "project",
    "task",
    "description",
)
SERVER_AUDIT_FIELDS = (
    "reviewed_by",
    "reviewed_on",
    "updated_tracker_log",
    "updated_by",
    "updated_on",
)


class TimeTrackerCorrectionRequest(Document):
    def before_insert(self):
        self.request_date = nowdate()
        self.requested_by = frappe.session.user
        self.workflow_state = REQUESTED
        self._clear_server_audit_fields()
        self._load_tracker_log_values(fill_requested_values=True)
        self._validate_requester()

    def validate(self):
        previous = self.get_doc_before_save()

        if self.is_new():
            self.request_date = nowdate()
            self.requested_by = frappe.session.user
            self.workflow_state = REQUESTED
            self.manager_remarks = None
            self._clear_server_audit_fields()
        elif previous:
            self._restore_server_audit_fields(previous)

        self._load_tracker_log_values(fill_requested_values=self.is_new())
        self._validate_immutable_fields(previous)
        self._validate_manager_remarks(previous)
        self._validate_tracker_log()
        self._validate_requested_values()
        self._validate_requested_context_permissions(previous)
        self._validate_transition(previous)
        self._validate_duplicate_open_request()

    def before_save(self):
        previous = self.get_doc_before_save()
        previous_state = previous.workflow_state if previous else None
        current_state = self.workflow_state or REQUESTED

        if previous_state == REQUESTED and current_state in {APPROVED, REJECTED}:
            self.reviewed_by = frappe.session.user
            self.reviewed_on = now_datetime()

        if previous_state == APPROVED and current_state == UPDATED:
            self._apply_correction_to_tracker_log()
            self.updated_tracker_log = self.tracker_log
            self.updated_by = frappe.session.user
            self.updated_on = now_datetime()

    @frappe.whitelist()
    def load_tracker_log_values(self):
        """Refresh a new request from a permitted, correctable Tracker Log."""

        if not self.is_new():
            frappe.throw(
                _("The Tracker Log cannot be changed after the request is created.")
            )

        self.check_permission("create")
        self.request_date = nowdate()
        self.requested_by = frappe.session.user
        self.workflow_state = REQUESTED

        # A user may switch the selected log before saving. Always preload the
        # requested values from the newly selected log so values from an earlier
        # selection cannot be carried into the request accidentally. User edits
        # made after this preload are preserved by normal validation on insert.
        for fieldname in REQUEST_VALUE_FIELDS:
            self.set(fieldname, None)

        self._load_tracker_log_values(fill_requested_values=True)
        self._validate_tracker_log()

        return {
            "request_date": self.request_date,
            "requested_by": self.requested_by,
            "workflow_state": self.workflow_state,
            "employee": self.employee,
            "employee_name": self.employee_name,
            "time_tracker": self.time_tracker,
            "current_log_date": self.current_log_date,
            "current_hours": self.current_hours,
            "current_project": self.current_project,
            "current_task": self.current_task,
            "current_description": self.current_description,
            "correction_date": self.correction_date,
            "total_hours": self.total_hours,
            "project": self.project,
            "task": self.task,
            "description": self.description,
        }

    def _load_tracker_log_values(self, *, fill_requested_values: bool) -> None:
        if not self.tracker_log:
            return

        log = frappe.db.get_value(
            "Tracker Log",
            self.tracker_log,
            [
                "employee",
                "time_tracker",
                "log_date",
                "hours",
                "project",
                "task",
                "description",
            ],
            as_dict=True,
        )
        if not log:
            return

        self.employee = log.employee
        self.employee_name = frappe.db.get_value(
            "Employee", log.employee, "employee_name"
        )
        self.time_tracker = log.time_tracker
        self.current_log_date = log.log_date
        self.current_hours = flt(log.hours, 6)
        self.current_project = log.project
        self.current_task = log.task
        self.current_description = log.description

        if fill_requested_values:
            self.correction_date = self.correction_date or log.log_date
            if not flt(self.total_hours):
                self.total_hours = flt(log.hours, 6)
            self.project = self.project or log.project
            self.task = self.task or log.task
            self.description = (
                self.description or log.description or _("Time correction")
            )

    def _validate_requester(self) -> None:
        if is_system_manager(frappe.session.user):
            return

        own_employee = get_employee_for_user(frappe.session.user, active_only=True)
        if not own_employee or self.employee != own_employee:
            frappe.throw(
                _("You may request a correction only for your own active Employee record."),
                frappe.PermissionError,
            )

    def _validate_immutable_fields(self, previous) -> None:
        if not previous:
            return

        changed_identity = [
            fieldname
            for fieldname in IDENTITY_FIELDS
            if self.has_value_changed(fieldname)
        ]
        if changed_identity:
            frappe.throw(
                _(
                    "These request identity fields cannot be changed after creation: {0}."
                ).format(", ".join(frappe.bold(field) for field in changed_identity))
            )

        old_state = previous.workflow_state or REQUESTED
        new_state = self.workflow_state or REQUESTED
        requester_edit = bool(
            old_state == REQUESTED
            and new_state == REQUESTED
            and self.requested_by == frappe.session.user
        )
        if requester_edit:
            return

        changed_values = [
            fieldname
            for fieldname in REQUEST_VALUE_FIELDS
            if self.has_value_changed(fieldname)
        ]
        if changed_values:
            frappe.throw(
                _(
                    "Requested correction values cannot be changed during manager "
                    "review or after approval: {0}."
                ).format(", ".join(frappe.bold(field) for field in changed_values))
            )

    def _validate_manager_remarks(self, previous) -> None:
        if not previous or not self.has_value_changed("manager_remarks"):
            return
        if not self._is_manager():
            frappe.throw(
                _("Only a manager can enter Manager Remarks."),
                frappe.PermissionError,
            )

    def _validate_tracker_log(self) -> None:
        if not self.tracker_log:
            frappe.throw(_("Tracker Log is required."))

        log = frappe.db.get_value(
            "Tracker Log",
            self.tracker_log,
            [
                "employee",
                "time_tracker",
                "status",
                "salary_slip",
                "start_time",
                "end_time",
                "ticket",
            ],
            as_dict=True,
        )
        if not log:
            frappe.throw(
                _("Tracker Log {0} does not exist.").format(
                    frappe.bold(self.tracker_log)
                )
            )

        if log.employee != self.employee or log.time_tracker != self.time_tracker:
            frappe.throw(_("The Tracker Log, Employee, and Time Tracker must match."))
        if log.status != "Stopped" or not log.start_time or not log.end_time:
            frappe.throw(_("Only a stopped Tracker Log can be corrected."))
        if log.salary_slip:
            frappe.throw(
                _(
                    "Cancel the linked Salary Slip {0} before requesting or applying "
                    "a correction to this Tracker Log."
                ).format(frappe.bold(log.salary_slip))
            )

        if self.is_new():
            self._validate_requester()

    def _validate_requested_values(self) -> None:
        if not self.correction_date:
            frappe.throw(_("Date for Correction is required."))
        if flt(self.total_hours) <= 0 or flt(self.total_hours) > 24:
            frappe.throw(
                _("Total Hours to Update must be greater than 0 and no more than 24.")
            )
        if not (self.description or "").strip():
            frappe.throw(_("Description is required."))

        for doctype, value in (("Project", self.project), ("Task", self.task)):
            if value and not frappe.db.exists(doctype, value):
                frappe.throw(
                    _("{0} {1} does not exist.").format(
                        doctype,
                        frappe.bold(value),
                    )
                )

        validate_task_project(self.project, self.task)

        ticket = frappe.db.get_value("Tracker Log", self.tracker_log, "ticket")
        if not any((self.project, self.task, ticket)):
            frappe.throw(
                _("Select a Project or Task because the Tracker Log has no Ticket context.")
            )

    def _validate_requested_context_permissions(self, previous) -> None:
        """Validate access when the requester changes Project or Task.

        Retaining the context already recorded on the employee's own Tracker
        Log is allowed. A newly selected Project or Task must be selectable by
        the requester at the time the request is created or edited. Manager
        workflow actions do not re-test the manager's unrelated record access.
        """

        if previous:
            old_state = previous.workflow_state or REQUESTED
            new_state = self.workflow_state or REQUESTED
            requester_edit = bool(
                old_state == REQUESTED
                and new_state == REQUESTED
                and self.requested_by == frappe.session.user
            )
            if not requester_edit:
                return

        for fieldname, doctype in (("project", "Project"), ("task", "Task")):
            value = self.get(fieldname)
            if not value:
                continue

            if previous:
                value_changed = self.has_value_changed(fieldname)
            else:
                value_changed = value != self.get(f"current_{fieldname}")

            if not value_changed:
                continue

            if not has_document_permission(doctype, value):
                frappe.throw(
                    _("You do not have permission to select {0} {1}.").format(
                        doctype,
                        frappe.bold(value),
                    ),
                    frappe.PermissionError,
                )

    def _validate_transition(self, previous) -> None:
        if not previous:
            if self.workflow_state != REQUESTED:
                frappe.throw(_("A new correction request must start in Requested state."))
            return

        old_state = previous.workflow_state or REQUESTED
        new_state = self.workflow_state or REQUESTED
        if old_state == new_state:
            if old_state in TERMINAL_STATES:
                frappe.throw(_("A rejected or updated correction request cannot be changed."))
            if old_state == REQUESTED and not self._can_edit_requested_state():
                frappe.throw(
                    _("Only the requester or a reviewing manager can edit this request."),
                    frappe.PermissionError,
                )
            if old_state == APPROVED and not self._is_manager():
                frappe.throw(
                    _("Only a manager can edit an approved correction request."),
                    frappe.PermissionError,
                )
            return

        allowed = {
            (REQUESTED, APPROVED),
            (REQUESTED, REJECTED),
            (APPROVED, UPDATED),
        }
        if (old_state, new_state) not in allowed:
            frappe.throw(
                _("Correction request cannot move from {0} to {1}.").format(
                    frappe.bold(old_state), frappe.bold(new_state)
                )
            )

        if not self._is_manager():
            frappe.throw(
                _("Only a manager can approve, reject, or apply a correction."),
                frappe.PermissionError,
            )
        if old_state == REQUESTED and self.requested_by == frappe.session.user:
            frappe.throw(
                _("You cannot approve or reject your own correction request."),
                frappe.PermissionError,
            )

    def _validate_duplicate_open_request(self) -> None:
        if not self.tracker_log or self.workflow_state not in {REQUESTED, APPROVED}:
            return

        duplicate = frappe.db.exists(
            "Time Tracker Correction Request",
            {
                "tracker_log": self.tracker_log,
                "workflow_state": ["in", [REQUESTED, APPROVED]],
                "name": ["!=", self.name or ""],
            },
        )
        if duplicate:
            frappe.throw(
                _("Tracker Log {0} already has open correction request {1}.").format(
                    frappe.bold(self.tracker_log), frappe.bold(duplicate)
                )
            )

    def _apply_correction_to_tracker_log(self) -> None:
        # Lock the source row so two requests cannot update it concurrently.
        frappe.db.sql(
            "SELECT name FROM `tabTracker Log` WHERE name = %s FOR UPDATE",
            self.tracker_log,
        )
        log = frappe.get_doc("Tracker Log", self.tracker_log)
        if log.salary_slip:
            frappe.throw(
                _("Cancel Salary Slip {0} before applying this correction.").format(
                    frappe.bold(log.salary_slip)
                )
            )
        if log.status != "Stopped" or not log.start_time:
            frappe.throw(_("Only a stopped Tracker Log can be updated."))

        original_start = get_datetime(log.start_time)
        corrected_start = datetime.combine(
            getdate(self.correction_date),
            original_start.time(),
        )
        corrected_end = corrected_start + timedelta(hours=flt(self.total_hours))

        log.log_date = getdate(self.correction_date)
        log.start_time = corrected_start
        log.end_time = corrected_end
        log.project = self.project or None
        log.task = self.task or None
        log.description = (self.description or "").strip()
        log.status = "Stopped"
        log.ended_by = frappe.session.user

        with time_tracker_log_write():
            log.save(ignore_permissions=True)

    def _clear_server_audit_fields(self) -> None:
        for fieldname in SERVER_AUDIT_FIELDS:
            self.set(fieldname, None)

    def _restore_server_audit_fields(self, previous) -> None:
        for fieldname in SERVER_AUDIT_FIELDS:
            self.set(fieldname, previous.get(fieldname))

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
