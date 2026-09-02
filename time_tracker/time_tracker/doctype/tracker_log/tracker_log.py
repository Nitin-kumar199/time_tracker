from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, time_diff_in_seconds

from time_tracker.permission_utils import doctype_exists, has_document_permission
from time_tracker.permissions import is_system_manager, is_tracker_log_editor
from time_tracker.time_tracker_security import is_time_tracker_log_write
from time_tracker.work_context import validate_task_project


TICKET_DOCTYPE = "HD Ticket"


class TrackerLog(Document):
    def before_insert(self):
        if not self._can_create_log():
            frappe.throw(
                _(
                    "Only System Manager can create a Tracker Log directly. "
                    "Other logs must be created from the Time Tracker."
                ),
                frappe.PermissionError,
            )

        if self.time_tracker and not self.employee:
            self.employee = frappe.db.get_value(
                "Time Tracker",
                self.time_tracker,
                "employee",
            )

        if not self.status:
            self.status = "Stopped" if self.end_time else "Running"

        if not self.started_by:
            self.started_by = frappe.session.user

        self._validate_work_context()

    def validate(self):
        if self.ticket:
            self.ticket_doctype = TICKET_DOCTYPE

        self._validate_write_source()
        self._validate_salary_slip_allocation()
        self._validate_work_context()

        if not self.time_tracker:
            frappe.throw(_("Time Tracker is required."))

        tracker_employee = frappe.db.get_value(
            "Time Tracker",
            self.time_tracker,
            "employee",
        )

        if not tracker_employee:
            frappe.throw(
                _("Time Tracker {0} does not exist.").format(
                    frappe.bold(self.time_tracker)
                )
            )

        if self.employee != tracker_employee:
            frappe.throw(
                _("The Tracker Log employee must match the Time Tracker employee.")
            )

        self._set_log_date()

        if self.status == "Running":
            duplicate_running = frappe.db.exists(
                "Tracker Log",
                {
                    "time_tracker": self.time_tracker,
                    "status": "Running",
                    "name": ["!=", self.name or ""],
                },
            )

            if duplicate_running:
                frappe.throw(_("This Time Tracker already has a running timer."))

            if not self.start_time:
                frappe.throw(_("Start Time is required."))

            self.end_time = None
            self.hours = 0

        elif self.status == "Stopped":
            if not self.start_time:
                frappe.throw(_("Start Time is required."))

            if not self.end_time:
                frappe.throw(_("End Time is required."))

            elapsed_seconds = time_diff_in_seconds(self.end_time, self.start_time)

            if elapsed_seconds < 0:
                frappe.throw(_("End Time cannot be before Start Time."))

            self.hours = flt(elapsed_seconds / 3600, 6)

        else:
            frappe.throw(_("Status must be Running or Stopped."))

    def _set_log_date(self) -> None:
        # The timer API supplies the Employee-local work date. Preserve it on
        # stop/edit; only legacy or directly-created logs need the system-date
        # fallback derived from the stored Start Time.
        if self.start_time and not self.log_date:
            self.log_date = getdate(self.start_time)

    def on_trash(self):
        if self.get("salary_slip"):
            frappe.throw(
                _(
                    "Cancel the linked Salary Slip before deleting this "
                    "Tracker Log."
                )
            )

        if not is_tracker_log_editor(frappe.session.user):
            frappe.throw(
                _("Only System Manager or Time Tracker Log Editor can delete logs."),
                frappe.PermissionError,
            )

    def _validate_salary_slip_allocation(self):
        """Keep payroll ownership automatic and paid time immutable."""

        previous = self.get_doc_before_save()
        current_salary_slip = self.get("salary_slip")

        if self.is_new():
            if current_salary_slip:
                frappe.throw(
                    _("Salary Slip links are managed automatically by payroll."),
                    frappe.PermissionError,
                )
            return

        if not previous:
            return

        previous_salary_slip = previous.get("salary_slip")

        if previous_salary_slip != current_salary_slip:
            frappe.throw(
                _("Salary Slip links are managed automatically by payroll."),
                frappe.PermissionError,
            )

        payroll_fields = (
            "time_tracker",
            "employee",
            "log_date",
            "start_time",
            "end_time",
            "hours",
            "status",
        )

        if previous_salary_slip and any(
            self.has_value_changed(fieldname) for fieldname in payroll_fields
        ):
            frappe.throw(
                _(
                    "Cancel the linked Salary Slip before changing the time "
                    "or status of this Tracker Log."
                )
            )

    def _validate_write_source(self):
        if self.is_new():
            if not self._can_create_log():
                frappe.throw(
                    _(
                        "Only System Manager can create a Tracker Log directly. "
                        "Other logs must be created from the Time Tracker."
                    ),
                    frappe.PermissionError,
                )
            return

        if not is_time_tracker_log_write() and not is_tracker_log_editor(
            frappe.session.user
        ):
            frappe.throw(
                _(
                    "Only System Manager or Time Tracker Log Editor can edit "
                    "an existing Tracker Log."
                ),
                frappe.PermissionError,
            )

    @staticmethod
    def _can_create_log() -> bool:
        return bool(
            is_time_tracker_log_write()
            or is_system_manager(frappe.session.user)
        )

    def _validate_work_context(self):
        if not any((self.project, self.task, self.ticket)):
            frappe.throw(
                _(
                    "Select at least one Project, Task, or Ticket before "
                    "starting a session."
                )
            )

        previous = self.get_doc_before_save()
        context_changed = self.is_new() or any(
            self.has_value_changed(fieldname)
            for fieldname in ("project", "task", "ticket")
        )

        # Stopping an existing timer must remain possible even when its Task was
        # later moved/deleted or Helpdesk was uninstalled. Permission and link
        # validation still runs whenever a context value is newly selected or
        # changed.
        if previous and not context_changed:
            return

        self._validate_context_permissions()
        validate_task_project(self.project, self.task)
        self._validate_ticket()

    def _validate_context_permissions(self) -> None:
        """Validate changed Project and Task values at record permission level."""

        previous = self.get_doc_before_save()
        trusted_write = is_time_tracker_log_write()

        for fieldname, doctype in (("project", "Project"), ("task", "Task")):
            value = self.get(fieldname)
            previous_value = previous.get(fieldname) if previous else None
            value_changed = self.is_new() or value != previous_value

            if not value or not value_changed:
                continue

            if not frappe.db.exists(doctype, value):
                frappe.throw(
                    _("{0} {1} does not exist.").format(
                        doctype,
                        frappe.bold(value),
                    )
                )

            # Timer actions validate the selected context before entering the
            # trusted write block. Correction requests validate the requesting
            # employee's access before manager approval. Requiring the server-
            # side approver to hold duplicate Project/Task access here would
            # prevent a valid approved correction from being applied.
            if trusted_write:
                continue

            if not has_document_permission(doctype, value):
                frappe.throw(
                    _("You do not have permission to select {0} {1}.").format(
                        doctype,
                        frappe.bold(value),
                    ),
                    frappe.PermissionError,
                )

    def _validate_ticket(self) -> None:
        if not self.ticket:
            return

        previous = self.get_doc_before_save()
        previous_ticket = previous.get("ticket") if previous else None
        ticket_changed = self.is_new() or previous_ticket != self.ticket

        if not doctype_exists(TICKET_DOCTYPE):
            if ticket_changed:
                frappe.throw(
                    _(
                        "HD Ticket is unavailable because Frappe Helpdesk is "
                        "not installed on this site."
                    )
                )
            return

        if not frappe.db.exists(TICKET_DOCTYPE, self.ticket):
            if ticket_changed:
                frappe.throw(
                    _("HD Ticket {0} does not exist.").format(
                        frappe.bold(self.ticket)
                    )
                )
            return

        if not ticket_changed:
            return

        if not has_document_permission(TICKET_DOCTYPE, self.ticket):
            frappe.throw(
                _("You do not have permission to select HD Ticket {0}.").format(
                    frappe.bold(self.ticket)
                ),
                frappe.PermissionError,
            )
