frappe.ui.form.on("Salary Structure", {
    refresh(frm) {
        update_time_tracker_salary_structure_ui(frm);
    },

    async custom_based_on_time_tracker(frm) {
        if (
            frm.doc.custom_based_on_time_tracker
            && frm.doc.salary_slip_based_on_timesheet
        ) {
            await frm.set_value("salary_slip_based_on_timesheet", 0);
        }
        update_time_tracker_salary_structure_ui(frm);
    },

    async salary_slip_based_on_timesheet(frm) {
        if (
            frm.doc.salary_slip_based_on_timesheet
            && frm.doc.custom_based_on_time_tracker
        ) {
            await frm.set_value("custom_based_on_time_tracker", 0);
        }
        update_time_tracker_salary_structure_ui(frm);
    },
});


function update_time_tracker_salary_structure_ui(frm) {
    if (!frm.fields_dict.custom_based_on_time_tracker) {
        return;
    }

    const usesTimeTracker = Boolean(frm.doc.custom_based_on_time_tracker);
    frm.set_df_property(
        "custom_based_on_time_tracker",
        "description",
        usesTimeTracker
            ? __(
                "This Salary Structure uses stopped Tracker Logs for hourly payroll. "
                + "The standard Timesheet payroll option remains disabled."
            )
            : __(
                "Enable this for Time Tracker payroll. It is separate from "
                + "Salary Slip Based on Timesheet."
            )
    );
}
