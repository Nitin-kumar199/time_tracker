from __future__ import annotations

import frappe


SALARY_SLIP_PRINT_FORMAT = "Time Tracker Salary Slip"
LEGACY_SALARY_SLIP_PRINT_FORMAT = "time_tracker Salary Slip"

SALARY_SLIP_PRINT_FORMAT_HTML = r"""
{% set employee = frappe.db.get_value("Employee", doc.employee, ["employee_name", "company", "department", "designation"], as_dict=True) if doc.employee else None %}
{% set worked_hours = doc.get("custom_time_tracker_hours") or doc.get("total_working_hours") or 0 %}
{% set payable_hours = doc.get("custom_total_monthly_hours") or doc.get("total_working_hours") or 0 %}
{% set exceeded_hours = doc.get("custom_total_exceeded_hours") or 0 %}
{% set hourly_rate = doc.get("custom_hourly_rate") or doc.get("hour_rate") or 0 %}
{% set payment_status = "Paid" if doc.docstatus == 1 else "Unpaid" %}
{% set weekly_summary = frappe.parse_json(doc.get("custom_weekly_time_tracker_summary_json") or "[]") %}

<div class="bw-print">
    <div class="bw-print-header">
        <div>
            <h1>Salary Slip</h1>
            <p>{{ doc.name }}</p>
        </div>
        <span class="bw-print-status bw-print-status-{{ payment_status | lower }}">
            {{ payment_status }}
        </span>
    </div>

    <div class="bw-print-period">
        <strong>Payroll period</strong>
        <span>{{ doc.get_formatted("start_date") }} &ndash; {{ doc.get_formatted("end_date") }}</span>
    </div>

    <table class="bw-print-details">
        <tbody>
            <tr>
                <th>Employee ID</th>
                <td>{{ doc.employee or "-" }}</td>
                <th>Employee Name</th>
                <td>{{ doc.employee_name or (employee.employee_name if employee else "-") }}</td>
            </tr>
            <tr>
                <th>Company</th>
                <td>{{ doc.company or (employee.company if employee else "-") }}</td>
                <th>Department</th>
                <td>{{ doc.department or (employee.department if employee else "-") or "-" }}</td>
            </tr>
            <tr>
                <th>Designation</th>
                <td>{{ doc.designation or (employee.designation if employee else "-") or "-" }}</td>
                <th>Posting Date</th>
                <td>{{ doc.get_formatted("posting_date") or "-" }}</td>
            </tr>
            <tr>
                <th>Payroll Entry</th>
                <td>{{ doc.payroll_entry or "-" }}</td>
                <th>Payroll Mode</th>
                <td>{{ doc.get("custom_time_tracking_source") or "Fixed Salary" }}</td>
            </tr>
        </tbody>
    </table>

    <h2>Hours and Pay Summary</h2>
    <table class="bw-print-summary">
        <thead>
            <tr>
                <th>Total Worked Hours</th>
                <th>Payable Hours</th>
                <th>Exceeded Hours</th>
                <th>Hourly Rate</th>
                <th>Total Paid Amount</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td>{{ frappe.utils.flt(worked_hours, 2) }}</td>
                <td>{{ frappe.utils.flt(payable_hours, 2) }}</td>
                <td>{{ frappe.utils.flt(exceeded_hours, 2) }}</td>
                <td>{{ frappe.utils.fmt_money(hourly_rate, currency=doc.currency) }}</td>
                <td>{{ frappe.utils.fmt_money(doc.net_pay or 0, currency=doc.currency) }}</td>
            </tr>
        </tbody>
    </table>

    <div class="bw-print-components">
        <div>
            <h2>Earnings</h2>
            <table>
                <thead>
                    <tr><th>Component</th><th class="text-right">Amount</th></tr>
                </thead>
                <tbody>
                    {% for row in doc.earnings %}
                    <tr>
                        <td>{{ row.salary_component }}</td>
                        <td class="text-right">{{ row.get_formatted("amount", doc) }}</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="2" class="bw-print-muted">No earnings</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        <div>
            <h2>Deductions</h2>
            <table>
                <thead>
                    <tr><th>Component</th><th class="text-right">Amount</th></tr>
                </thead>
                <tbody>
                    {% for row in doc.deductions %}
                    <tr>
                        <td>{{ row.salary_component }}</td>
                        <td class="text-right">{{ row.get_formatted("amount", doc) }}</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="2" class="bw-print-muted">No deductions</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <table class="bw-print-totals">
        <tbody>
            <tr>
                <th>Gross Pay</th>
                <td>{{ doc.get_formatted("gross_pay") }}</td>
            </tr>
            <tr>
                <th>Total Deduction</th>
                <td>{{ doc.get_formatted("total_deduction") }}</td>
            </tr>
            <tr class="bw-print-net">
                <th>Net Pay / Total Paid Amount</th>
                <td>{{ doc.get_formatted("net_pay") }}</td>
            </tr>
        </tbody>
    </table>

    {% if weekly_summary %}
    <h2>Weekly Time Tracker Summary</h2>
    <table class="bw-print-weekly">
        <thead>
            <tr>
                <th>Week</th>
                <th>Period</th>
                <th class="text-right">Tracked</th>
                <th class="text-right">Payable</th>
                <th class="text-right">Exceeded</th>
            </tr>
        </thead>
        <tbody>
            {% for row in weekly_summary %}
            <tr>
                <td>{{ row.week }}</td>
                <td>{{ frappe.utils.formatdate(row.period_start) }} &ndash; {{ frappe.utils.formatdate(row.period_end) }}</td>
                <td class="text-right">{{ frappe.utils.flt(row.tracked_hours, 2) }}</td>
                <td class="text-right">{{ frappe.utils.flt(row.payable_hours, 2) }}</td>
                <td class="text-right">{{ frappe.utils.flt(row.exceeded_hours, 2) }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
    {% endif %}

    <p class="bw-print-footer">
        Generated from {{ doc.doctype }} {{ doc.name }}.
    </p>
</div>
""".strip()

SALARY_SLIP_PRINT_FORMAT_CSS = r"""
.bw-print {
    color: #182230;
    font-size: 11px;
    line-height: 1.45;
}

.bw-print h1,
.bw-print h2,
.bw-print p {
    margin: 0;
}

.bw-print h1 {
    font-size: 24px;
    letter-spacing: -0.02em;
}

.bw-print h2 {
    font-size: 13px;
    margin: 18px 0 7px;
}

.bw-print-header,
.bw-print-period,
.bw-print-components {
    display: flex;
    justify-content: space-between;
}

.bw-print-header {
    align-items: flex-start;
    border-bottom: 2px solid #344054;
    padding-bottom: 12px;
}

.bw-print-header p,
.bw-print-muted,
.bw-print-footer {
    color: #667085;
}

.bw-print-status {
    border: 1px solid currentColor;
    border-radius: 999px;
    font-weight: 700;
    padding: 5px 12px;
}

.bw-print-status-paid {
    color: #067647;
}

.bw-print-status-unpaid {
    color: #b42318;
}

.bw-print-period {
    background: #f2f4f7;
    margin: 12px 0;
    padding: 8px 10px;
}

.bw-print table {
    border-collapse: collapse;
    width: 100%;
}

.bw-print th,
.bw-print td {
    border: 1px solid #d0d5dd;
    padding: 7px 8px;
    vertical-align: top;
}

.bw-print th {
    background: #f9fafb;
    font-weight: 700;
    text-align: left;
}

.bw-print-details th {
    width: 16%;
}

.bw-print-details td {
    width: 34%;
}

.bw-print-summary th,
.bw-print-summary td {
    text-align: center;
}

.bw-print-components {
    gap: 14px;
}

.bw-print-components > div {
    width: 49%;
}

.bw-print-totals {
    margin-left: auto;
    margin-top: 16px;
    width: 48% !important;
}

.bw-print-totals td {
    text-align: right;
}

.bw-print-net th,
.bw-print-net td {
    background: #eef4ff;
    font-size: 12px;
    font-weight: 700;
}

.bw-print-footer {
    border-top: 1px solid #d0d5dd;
    font-size: 9px;
    margin-top: 22px !important;
    padding-top: 8px;
    text-align: center;
}

.text-right {
    text-align: right !important;
}
""".strip()


def ensure_salary_slip_print_format() -> str | None:
    """Create or update the Time Tracker Salary Slip Jinja print format."""

    if not (
        frappe.db.exists("DocType", "Print Format")
        and frappe.db.exists("DocType", "Salary Slip")
    ):
        return None

    values = {
        "print_format_for": "DocType",
        "doc_type": "Salary Slip",
        "module": "Time Tracker",
        "standard": "No",
        "custom_format": 1,
        "print_format_type": "Jinja",
        "raw_printing": 0,
        "disabled": 0,
        "html": SALARY_SLIP_PRINT_FORMAT_HTML,
        "css": SALARY_SLIP_PRINT_FORMAT_CSS,
        "margin_top": 12,
        "margin_bottom": 12,
        "margin_left": 12,
        "margin_right": 12,
        "page_number": "Bottom Right",
    }

    if frappe.db.exists("Print Format", SALARY_SLIP_PRINT_FORMAT):
        print_format = frappe.get_doc(
            "Print Format",
            SALARY_SLIP_PRINT_FORMAT,
        )
        print_format.update(values)
        print_format.flags.ignore_permissions = True
        print_format.save(ignore_permissions=True)
    else:
        print_format = frappe.get_doc(
            {
                "doctype": "Print Format",
                "name": SALARY_SLIP_PRINT_FORMAT,
                **values,
            }
        )
        print_format.insert(ignore_permissions=True)

    frappe.clear_cache(doctype="Salary Slip")
    return print_format.name
