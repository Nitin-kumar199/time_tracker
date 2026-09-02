frappe.ui.form.on("Time Tracker Setting", {
    refresh(frm) {
        if (frm.is_new() || !frm.doc.enable_auto_create_monthly_payroll) {
            return;
        }

        frm.add_custom_button(
            __("Generate Previous Month Payroll Now"),
            async () => {
                const response = await frm.call("generate_previous_month_payroll_now");
                const result = response.message || {};
                const errors = result.errors || [];
                frappe.msgprint({
                    title: errors.length
                        ? __("Payroll Completed with Errors")
                        : __("Monthly Time Tracker Payroll"),
                    message: [
                        __("Company: {0}", [result.company || frm.doc.company]),
                        __("Status: {0}", [result.status || __("Completed")]),
                        __("Payroll Entry: {0}", [result.payroll_entry || __("Not created")]),
                        __("Employees: {0}", [result.employees || 0]),
                        __("Salary Slips: {0}", [result.salary_slips || 0]),
                    ].join("<br>"),
                    indicator: errors.length ? "orange" : "green",
                });
            },
            __("Automation")
        );
    },
});
