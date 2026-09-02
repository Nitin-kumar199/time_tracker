from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from time_tracker.permissions import (
    get_employee_for_user,
    get_employees_for_user,
    is_system_manager,
)
from time_tracker.time_tracker_security import is_employee_tracker_sync


class TimeTracker(Document):
    def before_insert(self):
        if is_employee_tracker_sync():
            if not self.employee:
                frappe.throw(_("Employee is required to provision a Time Tracker."))
            return

        if not self.employee:
            self.employee = get_employee_for_user(
                frappe.session.user
            )

        if not self.employee:
            frappe.throw(
                _(
                    "Link the current User to an Employee "
                    "before creating a Time Tracker."
                )
            )

        if not is_system_manager(frappe.session.user):
            own_employees = set(
                get_employees_for_user(
                    frappe.session.user,
                    active_only=True,
                )
            )

            if self.employee not in own_employees:
                frappe.throw(
                    _(
                        "You may create a Time Tracker only "
                        "for your own active Employee record."
                    ),
                    frappe.PermissionError,
                )

    def validate(self):
        employee_status = frappe.db.get_value(
            "Employee",
            self.employee,
            "status",
        )

        self.status = employee_status or "Inactive"

        # New trackers may only be created for active Employees. Existing
        # trackers remain visible and automatically show the Employee status.
        if (
            self.is_new()
            and employee_status != "Active"
            and not is_employee_tracker_sync()
        ):
            frappe.throw(
                _("Time Trackers can only be created for active Employees.")
            )

        previous = self.get_doc_before_save()

        if previous and previous.employee != self.employee:
            frappe.throw(
                _(
                    "The Employee cannot be changed after "
                    "the Time Tracker is created."
                )
            )

        existing = frappe.db.get_value(
            "Time Tracker",
            {
                "employee": self.employee,
            },
            "name",
        )

        if existing and existing != self.name:
            frappe.throw(
                _(
                    "Employee {0} already has Time Tracker {1}."
                ).format(
                    frappe.bold(self.employee),
                    frappe.bold(existing),
                )
            )