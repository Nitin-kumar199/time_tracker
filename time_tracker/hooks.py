app_name = "time_tracker"
app_title = "Time Tracker"
app_publisher = "Nitin Kumar"
app_description = "Employee time tracking, correction approvals, and payroll integration"
app_email = "199nitinkumar@gmail.com"
app_license = "MIT"

required_apps = ["erpnext", "hrms"]

app_include_js = ["/assets/time_tracker/js/time_tracker_widget.js"]
app_include_css = ["/assets/time_tracker/css/time_tracker_widget.css"]

before_install = "time_tracker.setup.before_install"
after_install = "time_tracker.setup.after_install"
before_migrate = "time_tracker.setup.before_migrate"
after_migrate = "time_tracker.setup.after_migrate"

doctype_js = {
    "Salary Structure": "public/js/salary_structure.js",
    "Payroll Entry": "public/js/payroll_entry.js",
    "Salary Slip": "public/js/salary_slip.js",
}

override_doctype_class = {
    "Payroll Entry": "time_tracker.overrides.payroll_entry.TimeTrackerPayrollEntry",
    "Salary Slip": "time_tracker.overrides.salary_slip.TimeTrackerSalarySlip",
}

permission_query_conditions = {
    "Time Tracker": "time_tracker.permissions.time_tracker_query",
    "Tracker Log": "time_tracker.permissions.tracker_log_query",
    "Time Tracker Correction Request": "time_tracker.permissions.correction_request_query",
}

has_permission = {
    "Time Tracker": "time_tracker.permissions.time_tracker_has_permission",
    "Tracker Log": "time_tracker.permissions.tracker_log_has_permission",
    "Time Tracker Correction Request": "time_tracker.permissions.correction_request_has_permission",
}

fixtures = [
    {
        "dt": "Custom Field",
        "filters": [[
            "name",
            "in",
            [
                "Employee-custom_hourly_rate_usd",
                "Employee-custom_weekly_hours_limit",
                "Employee-custom_time_tracker_hourly_rate",
                "Salary Structure-custom_based_on_time_tracker",
                "Payroll Entry-custom_pay_using_time_tracker",
                "Payroll Entry-custom_time_tracker_automation_mode",
                "Salary Slip-custom_time_tracker_section",
                "Salary Slip-custom_time_tracking_source",
                "Salary Slip-custom_payroll_hours_source",
                "Salary Slip-custom_timesheet",
                "Salary Slip-custom_time_tracker",
                "Salary Slip-custom_time_tracker_hours",
                "Salary Slip-custom_time_tracker_log_count",
                "Salary Slip-custom_hourly_rate",
                "Salary Slip-custom_total_monthly_hours",
                "Salary Slip-custom_total_exceeded_hours",
                "Salary Slip-custom_weekly_time_tracker_summary_json",
            ],
        ]],
    },
    {
        "dt": "Role",
        "filters": [[
            "name",
            "in",
            [
                "Time Tracker User",
                "Time Tracker Manager",
                "Time Tracker Log Editor",
            ],
        ]],
    },
]

doc_events = {
    "Employee": {
        "validate": "time_tracker.events.employee.sync_employee_weekly_hours_limit",
        "after_insert": "time_tracker.automation.handle_new_employee",
        "on_update": "time_tracker.events.employee.sync_time_tracker_status",
    },
    "Salary Structure": {
        "validate": "time_tracker.payroll.validate_salary_structure_time_tracker_mode",
    },
    "Salary Structure Assignment": {
        "on_submit": "time_tracker.automation.handle_salary_structure_assignment_change",
        "on_cancel": "time_tracker.automation.handle_salary_structure_assignment_change",
    },
    "Salary Slip": {
        "after_insert": "time_tracker.events.salary_slip.sync_time_tracker_log_links",
        "before_submit": "time_tracker.events.salary_slip.lock_time_tracker_logs_before_submit",
        "on_update": "time_tracker.events.salary_slip.sync_time_tracker_log_links",
        "on_submit": "time_tracker.events.salary_slip.sync_time_tracker_log_links",
        "on_cancel": "time_tracker.events.salary_slip.release_time_tracker_log_links",
        "on_trash": "time_tracker.events.salary_slip.release_time_tracker_log_links",
    },
    "Payroll Entry": {
        "on_submit": "time_tracker.payroll.reconcile_time_tracker_salary_slips_after_payroll_submit",
        "before_cancel": "time_tracker.payroll.release_time_tracker_logs_for_payroll_entry",
    },
}

scheduler_events = {
    "daily": [
        "time_tracker.automation.sync_time_trackers_from_salary_structures"
    ],
    "monthly_long": [
        "time_tracker.automation.generate_monthly_payroll"
    ],
}

override_doctype_dashboards = {
    "Employee": "time_tracker.overrides.employee_dashboard.get_dashboard_data",
}