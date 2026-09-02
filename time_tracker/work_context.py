from __future__ import annotations

import frappe
from frappe import _


def validate_task_project(project: str | None, task: str | None) -> None:
    """Require a selected Task to belong to the selected Project."""

    if not task:
        return

    if not project:
        frappe.throw(_("Select a Project before selecting a Task."))

    if not frappe.db.exists("Project", project):
        frappe.throw(
            _("Project {0} does not exist.").format(frappe.bold(project))
        )

    if not frappe.db.exists("Task", task):
        frappe.throw(_("Task {0} does not exist.").format(frappe.bold(task)))

    task_project = frappe.db.get_value("Task", task, "project")

    if not task_project:
        frappe.throw(
            _("Task {0} is not linked to a Project.").format(frappe.bold(task))
        )

    if task_project != project:
        frappe.throw(
            _("Task {0} belongs to project {1}, not {2}.").format(
                frappe.bold(task),
                frappe.bold(task_project),
                frappe.bold(project),
            )
        )
