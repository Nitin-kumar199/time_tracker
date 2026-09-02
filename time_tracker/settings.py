from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint


SETTINGS_DOCTYPE = "Time Tracker Setting"
DEFAULT_PRINT_FORMAT = "Time Tracker Salary Slip"


def get_company_setting(
    company: str | None,
    *,
    enabled_only: bool = False,
):
    if not company or not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
        return None

    filters: dict[str, object] = {"company": company}
    if enabled_only:
        filters["enabled"] = 1

    name = frappe.db.get_value(SETTINGS_DOCTYPE, filters, "name")
    return frappe.get_doc(SETTINGS_DOCTYPE, name) if name else None


def get_employee_setting(employee: str | None, *, enabled_only: bool = False):
    if not employee:
        return None
    company = frappe.db.get_value("Employee", employee, "company")
    return get_company_setting(company, enabled_only=enabled_only)


def get_enabled_settings(*, company: str | None = None) -> list:
    if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
        return []

    filters: dict[str, object] = {"enabled": 1}
    if company:
        filters["company"] = company

    return [
        frappe.get_doc(SETTINGS_DOCTYPE, name)
        for name in frappe.get_all(
            SETTINGS_DOCTYPE,
            filters=filters,
            pluck="name",
            order_by="company asc",
            limit_page_length=0,
        )
    ]


def get_time_tracker_ui_settings(company: str | None) -> frappe._dict:
    setting = get_company_setting(company)
    return frappe._dict(
        show_salary_slip_on_time_tracker=(
            bool(cint(setting.show_salary_slip_on_time_tracker)) if setting else True
        ),
        show_pay_using_time_tracker_in_payroll_entry=(
            bool(cint(setting.show_pay_using_time_tracker_in_payroll_entry))
            if setting
            else True
        ),
        salary_slip_print_format=(
            setting.salary_slip_print_format
            if setting and setting.salary_slip_print_format
            else DEFAULT_PRINT_FORMAT
        ),
    )


@frappe.whitelist()
def get_payroll_entry_ui_settings(company: str | None = None) -> dict:
    if frappe.session.user == "Guest":
        frappe.throw(_("Please sign in."), frappe.PermissionError)

    values = get_time_tracker_ui_settings(company)
    return {
        "show_pay_using_time_tracker": bool(
            values.show_pay_using_time_tracker_in_payroll_entry
        )
    }
