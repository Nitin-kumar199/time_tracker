from __future__ import annotations

from frappe.utils import cint

from time_tracker.payroll import (
    allocate_time_tracker_logs,
    prepare_time_tracker_hours,
    release_time_tracker_logs,
    uses_time_tracker_salary_slip,
)


def lock_time_tracker_logs_before_submit(doc, method: str | None = None) -> None:
    """Lock and recalculate the exact Tracker Logs used on submission.

    This is a document event instead of relying only on the Salary Slip class
    override. It therefore also runs when another installed app replaces the
    Salary Slip controller after Time Tracker in the hooks resolution order.
    """

    del method

    if uses_time_tracker_salary_slip(doc):
        prepare_time_tracker_hours(doc, lock=True, recalculate=True)


def sync_time_tracker_log_links(doc, method: str | None = None) -> None:
    """Allocate/reconcile Tracker Logs after Draft or Submitted slip saves.

    Frappe runs this after insert, on update, and again during submit. Allocation
    is deliberately idempotent, so the additional ``on_submit`` hook is a safe
    fallback for integrations that invoke that lifecycle event directly. No
    persistent in-memory flag is used: repeated saves of the same Document
    instance must always be able to refresh its allocation.
    """

    del method

    if cint(doc.get("docstatus")) == 2:
        release_time_tracker_logs(doc)
    elif uses_time_tracker_salary_slip(doc):
        allocate_time_tracker_logs(doc)
    else:
        # A Draft Salary Slip may be edited away from Time Tracker mode.
        release_time_tracker_logs(doc)


def release_time_tracker_log_links(doc, method: str | None = None) -> None:
    """Release payroll allocations before cancellation or deletion."""

    del method
    release_time_tracker_logs(doc)
