from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_months, cint, flt, get_first_day, get_last_day, getdate, nowdate


class TimeTrackerSetting(Document):
    def validate(self):
        if not self.currency and self.company:
            self.currency = frappe.db.get_value("Company", self.company, "default_currency")

        self._validate_default_salary_structure()
        self._validate_income_tax_slab()
        self._validate_appraisal_cycle()
        self._validate_leave_configuration()
        self._validate_company_link("payroll_payable_account", "Account")
        self._validate_company_link("cost_center", "Cost Center")
        self._validate_print_format()
        self._validate_monthly_payroll()

    @frappe.whitelist()
    def generate_previous_month_payroll_now(self):
        self.check_permission("write")
        if not cint(self.enabled) or not cint(self.enable_auto_create_monthly_payroll):
            frappe.throw(
                _("Enable this setting and monthly payroll automation before running it.")
            )

        from time_tracker.automation import generate_company_time_tracker_payroll_for_period

        anchor = add_months(getdate(nowdate()), -1)
        return generate_company_time_tracker_payroll_for_period(
            self.company,
            get_first_day(anchor),
            get_last_day(anchor),
        )

    def _validate_default_salary_structure(self) -> None:
        if not self.default_salary_structure:
            return

        structure = frappe.db.get_value(
            "Salary Structure",
            self.default_salary_structure,
            ["company", "currency", "docstatus", "is_active"],
            as_dict=True,
        )
        if not structure:
            return

        if (
            structure.company != self.company
            or (self.currency and structure.currency != self.currency)
            or cint(structure.docstatus) != 1
            or structure.is_active != "Yes"
        ):
            frappe.throw(
                _(
                    "Salary Structure {0} must be submitted, active, and use "
                    "Company {1} with Currency {2}."
                ).format(
                    frappe.bold(self.default_salary_structure),
                    frappe.bold(self.company),
                    frappe.bold(self.currency or _("Not set")),
                )
            )

    def _validate_income_tax_slab(self) -> None:
        if not self.income_tax_slab:
            return

        slab = frappe.db.get_value(
            "Income Tax Slab",
            self.income_tax_slab,
            ["currency", "docstatus"],
            as_dict=True,
        )
        if slab and (
            cint(slab.docstatus) != 1
            or (self.currency and slab.currency != self.currency)
        ):
            frappe.throw(
                _("Income Tax Slab {0} must be submitted and use Currency {1}.").format(
                    frappe.bold(self.income_tax_slab),
                    frappe.bold(self.currency or _("Not set")),
                )
            )

    def _validate_appraisal_cycle(self) -> None:
        if not self.appraisal_cycle:
            return

        cycle = frappe.db.get_value(
            "Appraisal Cycle",
            self.appraisal_cycle,
            ["company", "status"],
            as_dict=True,
        )
        if not cycle:
            return
        if cycle.company and cycle.company != self.company:
            frappe.throw(
                _("Appraisal Cycle {0} belongs to Company {1}, not {2}.").format(
                    frappe.bold(self.appraisal_cycle),
                    frappe.bold(cycle.company),
                    frappe.bold(self.company),
                )
            )
        if cycle.status == "Completed":
            frappe.throw(
                _("Appraisal Cycle {0} is completed.").format(
                    frappe.bold(self.appraisal_cycle)
                )
            )

    def _validate_leave_configuration(self) -> None:
        if self.leave_policy:
            docstatus = frappe.db.get_value("Leave Policy", self.leave_policy, "docstatus")
            if docstatus is not None and cint(docstatus) != 1:
                frappe.throw(
                    _("Leave Policy {0} must be submitted.").format(
                        frappe.bold(self.leave_policy)
                    )
                )

        if (
            self.leave_policy
            and self.leave_assignment_based_on == "Leave Period"
            and not self.leave_period
        ):
            frappe.throw(
                _("Select a Leave Period for Company {0}.").format(
                    frappe.bold(self.company)
                )
            )

        if not self.leave_period:
            return

        period = frappe.db.get_value(
            "Leave Period",
            self.leave_period,
            ["company", "is_active"],
            as_dict=True,
        )
        if period and period.company and period.company != self.company:
            frappe.throw(
                _("Leave Period {0} belongs to Company {1}, not {2}.").format(
                    frappe.bold(self.leave_period),
                    frappe.bold(period.company),
                    frappe.bold(self.company),
                )
            )

    def _validate_company_link(self, fieldname: str, doctype: str) -> None:
        value = self.get(fieldname)
        if not value:
            return

        values = frappe.db.get_value(
            doctype,
            value,
            ["company", "is_group"],
            as_dict=True,
        )
        if not values:
            return
        if values.company and values.company != self.company:
            frappe.throw(
                _("{0} {1} belongs to Company {2}, not {3}.").format(
                    _(doctype),
                    frappe.bold(value),
                    frappe.bold(values.company),
                    frappe.bold(self.company),
                )
            )
        if cint(values.get("is_group")):
            frappe.throw(
                _("{0} {1} cannot be a group.").format(_(doctype), frappe.bold(value))
            )

    def _validate_print_format(self) -> None:
        if not self.salary_slip_print_format:
            from time_tracker.print_formats import SALARY_SLIP_PRINT_FORMAT

            self.salary_slip_print_format = SALARY_SLIP_PRINT_FORMAT
            return

        doc_type = frappe.db.get_value(
            "Print Format", self.salary_slip_print_format, "doc_type"
        )
        if doc_type and doc_type != "Salary Slip":
            frappe.throw(
                _("Print Format {0} must be for Salary Slip.").format(
                    frappe.bold(self.salary_slip_print_format)
                )
            )

    def _validate_monthly_payroll(self) -> None:
        if not cint(self.enable_auto_create_monthly_payroll):
            return

        missing = [
            label
            for fieldname, label in (
                ("currency", _("Currency")),
                ("payroll_payable_account", _("Payroll Payable Account")),
                ("cost_center", _("Cost Center")),
            )
            if not self.get(fieldname)
        ]
        if missing:
            frappe.throw(
                _("Complete monthly payroll settings for Company {0}: {1}.").format(
                    frappe.bold(self.company), ", ".join(missing)
                )
            )

        if flt(self.exchange_rate) <= 0:
            frappe.throw(
                _("Exchange Rate must be positive for Company {0}.").format(
                    frappe.bold(self.company)
                )
            )
