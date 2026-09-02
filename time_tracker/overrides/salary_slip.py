from __future__ import annotations

from hrms.payroll.doctype.salary_slip.salary_slip import SalarySlip

from time_tracker.payroll import (
    apply_time_tracker_hourly_earning,
    check_duplicate_time_tracker_salary_slip,
    initialise_time_tracker_salary_slip,
    prepare_time_tracker_hours,
    pull_time_tracker_salary_structure,
    select_automated_fixed_salary_structure,
    select_time_tracker_salary_structure,
    set_time_tracker_salary_structure_assignment,
    uses_automated_fixed_salary_slip,
    uses_time_tracker_salary_slip,
)


class TimeTrackerSalarySlip(SalarySlip):
    """Use Time Tracker Tracker Logs as the hourly payroll source."""

    def check_existing(self):
        if uses_time_tracker_salary_slip(self):
            check_duplicate_time_tracker_salary_slip(self)
            return

        return super().check_existing()

    def check_sal_struct(self):
        if uses_time_tracker_salary_slip(self):
            return select_time_tracker_salary_structure(self)

        if uses_automated_fixed_salary_slip(self):
            return select_automated_fixed_salary_structure(self)

        return super().check_sal_struct()

    def set_time_sheet(self):
        if uses_time_tracker_salary_slip(self):
            prepare_time_tracker_hours(self, recalculate=False)
            return

        return super().set_time_sheet()

    def add_timesheet_earning_component(self, timesheet_config):
        """Keep capped Tracker Log hours on HRMS v16 and newer.

        HRMS v16 invokes this method after ``set_time_sheet`` and its standard
        implementation replaces ``total_working_hours`` with the sum of the
        ERPNext Timesheet child table. Time Tracker intentionally leaves that
        table empty, so use the already-calculated Tracker Log payable hours.
        """

        if uses_time_tracker_salary_slip(self):
            salary_component = getattr(
                timesheet_config,
                "timesheet_component",
                None,
            )
            apply_time_tracker_hourly_earning(
                self,
                salary_component=salary_component,
            )
            return

        parent = getattr(super(), "add_timesheet_earning_component", None)
        if parent:
            return parent(timesheet_config)

    def pull_sal_struct(self):
        if uses_time_tracker_salary_slip(self):
            pull_time_tracker_salary_structure(self)
            return

        return super().pull_sal_struct()

    def set_salary_structure_assignment(self):
        if uses_time_tracker_salary_slip(self):
            set_time_tracker_salary_structure_assignment(self)
            return

        return super().set_salary_structure_assignment()

    def validate(self):
        uses_tracker = uses_time_tracker_salary_slip(self)

        if uses_tracker:
            # Establish the explicit assigned hourly structure before HRMS
            # decides whether it needs to load/reload earnings and deductions.
            initialise_time_tracker_salary_slip(self, repair_draft=True)

        generated_tables_were_loaded = bool(
            (self.get("earnings") or []) or (self.get("deductions") or [])
        )
        super().validate()

        if uses_tracker and generated_tables_were_loaded:
            # On later edits HRMS does not reload the Salary Structure when the
            # earnings table is already populated, so refresh selected source
            # hours and totals after every normal validation as well.
            prepare_time_tracker_hours(self, recalculate=True)
