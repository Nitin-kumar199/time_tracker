frappe.ui.form.on("Tracker Log", {
    setup(frm) {
        // Optional context fields remain hidden until the server confirms the
        // current user's DocType permissions. This avoids briefly exposing a
        // field while the asynchronous permission probe is running.
        frm.toggle_display("project", false);
        frm.toggle_display("task", false);
        frm.toggle_display("ticket", false);

        frm.set_query("task", () => ({
            filters: frm.doc.project
                ? { project: frm.doc.project }
                : { name: ["=", "__no_project_selected__"] },
        }));
    },

    async onload(frm) {
        await configure_work_context_fields(frm);
    },

    async refresh(frm) {
        await configure_work_context_fields(frm);
        show_creation_notice(frm);
        add_correction_request_button(frm);
    },

    project(frm) {
        if (frm.doc.task) {
            frm.set_value("task", null);
        }

        toggle_task_field(frm);
    },
});


async function configure_work_context_fields(frm) {
    try {
        const response = await frappe.call({
            method: "time_tracker.api.get_work_context_permissions",
            type: "POST",
            freeze: false,
        });
        const data = response.message || {};
        const permissions = data.permissions || {};

        frm.__bw_context_permissions = permissions;
        frm.__bw_ticket_doctype = data.ticket_doctype || "";

        frm.toggle_display("project", Boolean(permissions.project));
        frm.toggle_display(
            "ticket",
            Boolean(permissions.ticket && frm.__bw_ticket_doctype)
        );

        toggle_task_field(frm);
    } catch (error) {
        console.error("Unable to load Tracker Log context permissions", error);

        // A failed permission probe must not expose optional context fields.
        frm.__bw_context_permissions = {};
        frm.toggle_display("project", false);
        frm.toggle_display("task", false);
        frm.toggle_display("ticket", false);
    }
}


function toggle_task_field(frm) {
    const permissions = frm.__bw_context_permissions || {};
    const mayUseTask = Boolean(
        permissions.project
        && permissions.task
        && frm.doc.project
    );

    frm.toggle_display("task", mayUseTask);

    const field = frm.fields_dict.task;
    if (field && field.$input) {
        field.$input.prop("disabled", !mayUseTask);
    }
}


function show_creation_notice(frm) {
    if (!frm.is_new()) {
        return;
    }

    const isSystemManager = (
        frappe.session.user === "Administrator"
        || frappe.user.has_role("System Manager")
    );

    frm.dashboard.set_headline_alert(
        isSystemManager
            ? __(
                "System Manager may create this Tracker Log directly. "
                + "Normal employee logs must still be recorded from Time Tracker."
            )
            : __(
                "Only System Manager can create a Tracker Log directly. "
                + "Use Time Tracker to record a work session."
            ),
        isSystemManager ? "blue" : "orange"
    );
}


function add_correction_request_button(frm) {
    if (
        frm.is_new()
        || frm.doc.status !== "Stopped"
        || frm.doc.salary_slip
        || !frappe.model.can_create("Time Tracker Correction Request")
    ) {
        return;
    }

    frm.add_custom_button(
        __("Request Correction"),
        () => {
            frappe.new_doc("Time Tracker Correction Request", {
                tracker_log: frm.doc.name,
            });
        },
        __("Actions")
    );
}
