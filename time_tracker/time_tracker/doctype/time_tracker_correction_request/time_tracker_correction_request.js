frappe.ui.form.on("Time Tracker Correction Request", {
    setup(frm) {
        frm.set_query("tracker_log", () => ({
            filters: {
                status: "Stopped",
                salary_slip: ["is", "not set"],
            },
        }));

        frm.set_query("task", () => ({
            filters: frm.doc.project
                ? { project: frm.doc.project }
                : { name: ["=", "__no_project_selected__"] },
        }));
    },

    async onload(frm) {
        if (frm.is_new() && frm.doc.tracker_log && !frm.doc.employee) {
            await load_selected_tracker_log(frm);
        }
    },

    refresh(frm) {
        update_correction_request_editability(frm);
    },

    workflow_state(frm) {
        update_correction_request_editability(frm);
    },

    async tracker_log(frm) {
        if (!frm.doc.tracker_log && frm.is_new()) {
            clear_loaded_tracker_log_values(frm);
            return;
        }
        await load_selected_tracker_log(frm);
    },

    project(frm) {
        if (frm.doc.task) {
            frm.set_value("task", null);
        }
    },
});


async function load_selected_tracker_log(frm) {
    if (!frm.doc.tracker_log || !frm.is_new()) {
        return;
    }

    const response = await frm.call("load_tracker_log_values");
    const values = response.message || {};
    Object.entries(values).forEach(([fieldname, value]) => {
        frm.doc[fieldname] = value;
    });
    frm.refresh_fields();
    update_correction_request_editability(frm);
}


function clear_loaded_tracker_log_values(frm) {
    const fields = [
        "employee",
        "employee_name",
        "time_tracker",
        "current_log_date",
        "current_hours",
        "current_project",
        "current_task",
        "current_description",
        "correction_date",
        "total_hours",
        "project",
        "task",
        "description",
    ];
    fields.forEach((fieldname) => {
        frm.doc[fieldname] = null;
    });
    frm.refresh_fields();
    update_correction_request_editability(frm);
}


function update_correction_request_editability(frm) {
    const state = String(frm.doc.workflow_state || "Requested");
    const requesterCanEdit = (
        frm.is_new()
        || (
            state === "Requested"
            && frm.doc.requested_by === frappe.session.user
        )
    );
    const requestFields = [
        "correction_date",
        "total_hours",
        "project",
        "task",
        "description",
    ];

    const reviewerRoles = [
        "Time Tracker Manager",
        "Time Tracker Log Editor",
        "HR Manager",
        "System Manager",
    ];
    const canReview = reviewerRoles.some((role) => frappe.user.has_role(role));

    frm.set_df_property("tracker_log", "read_only", !frm.is_new());
    frm.set_df_property("manager_remarks", "read_only", !canReview);
    requestFields.forEach((fieldname) => {
        if (frm.fields_dict[fieldname]) {
            frm.set_df_property(fieldname, "read_only", !requesterCanEdit);
        }
    });
}
