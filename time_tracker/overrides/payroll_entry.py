from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import frappe
from frappe import _
from hrms.payroll.doctype.payroll_entry.payroll_entry import PayrollEntry

from time_tracker.payroll import (
    build_time_tracker_payroll_diagnostics,
    filter_fixed_salary_payroll_employee_rows,
    filter_time_tracker_payroll_employee_rows,
    reconcile_time_tracker_salary_slips_for_payroll_entry,
    uses_automated_fixed_salary_payroll_entry,
    uses_time_tracker_payroll_entry,
    validate_automated_fixed_salary_payroll_entry,
    validate_time_tracker_automation_mode,
    validate_time_tracker_payroll_entry,
    validate_time_tracker_payroll_mode,
)


class TimeTrackerPayrollEntry(PayrollEntry):
    """Add a Time Tracker hourly source without reusing the Timesheet UI.

    HRMS identifies hourly Salary Structures and generated hourly Salary Slips
    through ``salary_slip_based_on_timesheet``. For a Time Tracker Payroll
    Entry, that standard flag is enabled only while the relevant HRMS methods
    execute. The saved Payroll Entry keeps its Timesheet checkbox clear and
    stores the independent ``custom_pay_using_time_tracker`` choice instead.
    """

    def uses_time_tracker_payroll(self) -> bool:
        return uses_time_tracker_payroll_entry(self)

    def uses_automated_fixed_salary_payroll(self) -> bool:
        return uses_automated_fixed_salary_payroll_entry(self)

    @contextmanager
    def _hourly_structure_mode(self) -> Iterator[None]:
        original_value = self.get("salary_slip_based_on_timesheet")
        self.salary_slip_based_on_timesheet = 1

        try:
            yield
        finally:
            self.salary_slip_based_on_timesheet = original_value

    def validate(self):
        validate_time_tracker_automation_mode(self)
        validate_time_tracker_payroll_mode(self)
        return super().validate()

    def before_submit(self):
        if self.uses_automated_fixed_salary_payroll():
            validate_automated_fixed_salary_payroll_entry(self)
        else:
            validate_time_tracker_payroll_entry(self)
        return super().before_submit()

    def make_filters(self):
        # Time Tracker Salary Structures deliberately keep ERPNext's standard
        # Timesheet flag off. Employee selection therefore uses the normal
        # non-Timesheet candidate path and applies the custom structure flag.
        return super().make_filters()

    @frappe.whitelist()
    def fill_employee_details(self):
        if self.uses_automated_fixed_salary_payroll():
            validate_time_tracker_automation_mode(self)

            from hrms.payroll.doctype.payroll_entry.payroll_entry import (
                get_employee_list,
            )

            filters = self.make_filters()
            employees = get_employee_list(
                filters=filters,
                as_dict=True,
                ignore_match_conditions=True,
            )
            employees = filter_fixed_salary_payroll_employee_rows(
                employees,
                filters,
                as_dict=True,
            )
            self.set("employees", [])

            if not employees:
                frappe.throw(
                    _(
                        "No Employees have a latest effective submitted fixed "
                        "Salary Structure Assignment matching this company, "
                        "currency, payroll frequency, date range, and Payroll "
                        "Payable Account."
                    ),
                    title=_("No Eligible Fixed Salary Employees"),
                )

            self.set("employees", employees)
            self.number_of_employees = len(self.employees)
            self.update_employees_with_withheld_salaries()
            return self.get_employees_with_unmarked_attendance()

        if not self.uses_time_tracker_payroll():
            return super().fill_employee_details()

        validate_time_tracker_payroll_mode(self)

        # Onboarding creates trackers only for effective hourly Salary
        # Structures. Filtering below also protects manually-created payroll.

        from hrms.payroll.doctype.payroll_entry.payroll_entry import (
            get_employee_list,
        )

        filters = self.make_filters()
        employees = get_employee_list(
            filters=filters,
            as_dict=True,
            ignore_match_conditions=True,
        )
        employees = filter_time_tracker_payroll_employee_rows(
            employees,
            filters,
            as_dict=True,
        )
        self.set("employees", [])

        if not employees:
            diagnostics = build_time_tracker_payroll_diagnostics(self)
            frappe.throw(
                diagnostics.html,
                title=_("No Eligible Time Tracker Employees"),
            )

        self.set("employees", employees)
        self.number_of_employees = len(self.employees)
        self.update_employees_with_withheld_salaries()
        attendance_result = self.get_employees_with_unmarked_attendance()

        return attendance_result

    @frappe.whitelist()
    def get_time_tracker_diagnostics(self):
        if self.is_new():
            if not frappe.has_permission(
                doctype=self.doctype,
                ptype="create",
                doc=self,
                user=frappe.session.user,
            ):
                frappe.throw(_("Not permitted."), frappe.PermissionError)
        else:
            self.check_permission("read")
        validate_time_tracker_payroll_mode(self)
        return build_time_tracker_payroll_diagnostics(self)

    @frappe.whitelist()
    def create_salary_slips(self):
        if not self.uses_time_tracker_payroll():
            return super().create_salary_slips()

        # This method can be retried independently after a failed background
        # job, so repeat the server validation here instead of relying only on
        # ``before_submit``.
        validate_time_tracker_payroll_entry(self)

        with self._hourly_structure_mode():
            result = super().create_salary_slips()

        # For the synchronous HRMS path this repairs links immediately. The
        # Salary Slip document events perform the same idempotent allocation in
        # queued workers, so payrolls with more than 30 Employees are covered.
        if self.name and not self.is_new():
            reconcile_time_tracker_salary_slips_for_payroll_entry(self.name)

        return result

    @frappe.whitelist()
    def relink_time_tracker_logs(self):
        """Manually reconcile links for slips already created by this payroll."""

        self.check_permission("write")
        if not self.uses_time_tracker_payroll():
            frappe.throw(_("This is not a Time Tracker Payroll Entry."))

        return reconcile_time_tracker_salary_slips_for_payroll_entry(self.name)

    def get_sal_slip_list(self, ss_status, as_dict=False):
        if not self.uses_time_tracker_payroll():
            return super().get_sal_slip_list(ss_status, as_dict=as_dict)

        # Generated Time Tracker Salary Slips intentionally carry HRMS's
        # internal hourly flag so existing submission and accounting queries
        # continue to include them.
        with self._hourly_structure_mode():
            return super().get_sal_slip_list(ss_status, as_dict=as_dict)
