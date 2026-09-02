const CORRECTION_EDITABLE_FIELDS = [
    "start_time",
    "hours",
    "project",
    "task",
    "ticket",
    "description",
];

frappe.ui.form.on("Time Tracker Correction Request", {
    setup(frm) {
        frm.set_query("task", "logs", (doc, cdt, cdn) => {
            const row = locals[cdt][cdn];
            return {
                filters: row.project
                    ? { project: row.project }
                    : { name: ["=", "__no_project_selected__"] },
            };
        });
    },

    async onload(frm) {
        await configure_correction_context_fields(frm);

        if (!frm.is_new()) {
            return;
        }

        await load_employee_context(frm);
        if (frm.doc.correction_date) {
            await load_tracker_logs_for_date(frm, { showMessage: false });
        }
    },

    async refresh(frm) {
        await configure_correction_context_fields(frm);
        update_correction_request_editability(frm);
        update_request_summary(frm);
        add_reload_logs_button(frm);
        // show_correction_status(frm);
    },

    workflow_state(frm) {
        update_correction_request_editability(frm);
        // show_correction_status(frm);
    },

    async correction_date(frm) {
        if (!frm.doc.correction_date || !requester_can_edit(frm)) {
            return;
        }

        await load_tracker_logs_for_date(frm, { showMessage: true });
    },

    logs_on_form_rendered(frm) {
        update_correction_request_editability(frm);
    },
});

frappe.ui.form.on("Time Tracker Correction Log", {
    before_logs_remove(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (row.tracker_log) {
            frappe.throw(
                __(
                    "Existing Tracker Log rows cannot be removed. "
                    + "Update their requested values instead."
                )
            );
        }
    },

    logs_add(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        frappe.model.set_value(cdt, cdn, "row_type", "New Log");
        frappe.model.set_value(cdt, cdn, "ticket_doctype", "HD Ticket");
        set_suggested_start_time(frm, row);
        update_request_summary(frm);
        update_correction_request_editability(frm);
    },

    logs_remove(frm) {
        update_request_summary(frm);
    },

    hours(frm) {
        update_request_summary(frm);
    },

    tracker_log(frm) {
        update_request_summary(frm);
    },

    project(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (row.task) {
            frappe.model.set_value(cdt, cdn, "task", null);
        }
    },
});


async function load_employee_context(frm) {
    if (!frm.is_new()) {
        return;
    }

    const response = await frm.call("load_employee_context");
    apply_parent_values(frm, response.message || {});
}


async function load_tracker_logs_for_date(frm, { showMessage = true } = {}) {
    if (!frm.doc.correction_date || frm.__loading_correction_logs) {
        return;
    }

    frm.__loading_correction_logs = true;
    try {
        const response = await frm.call("load_tracker_logs");
        const values = response.message || {};
        apply_parent_values(frm, values);
        replace_log_rows(frm, values.logs || []);
        update_request_summary(frm);
        update_correction_request_editability(frm);

        const runningLogs = values.running_logs || [];
        if (runningLogs.length) {
            frappe.msgprint({
                title: __("Running Tracker Log Not Included"),
                indicator: "orange",
                message: __(
                    "The following running logs cannot be corrected until they are stopped: {0}",
                    [
                        runningLogs
                            .map(
                                (name) => `<strong>${frappe.utils.escape_html(name)}</strong>`
                            )
                            .join(", "),
                    ]
                ),
            });
        } else if (showMessage && !(values.logs || []).length) {
            frappe.show_alert({
                message: __(
                    "No stopped Tracker Logs exist for this date. "
                    + "Add a row for the missing time."
                ),
                indicator: "blue",
            });
        }
    } finally {
        frm.__loading_correction_logs = false;
    }
}


function apply_parent_values(frm, values) {
    const childOnly = new Set(["logs", "running_logs"]);
    Object.entries(values).forEach(([fieldname, value]) => {
        if (!childOnly.has(fieldname) && frm.fields_dict[fieldname]) {
            frm.doc[fieldname] = value;
        }
    });
    frm.refresh_fields();
}


function replace_log_rows(frm, sourceRows) {
    frappe.model.clear_table(frm.doc, "logs");

    sourceRows.forEach((source) => {
        const row = frappe.model.add_child(
            frm.doc,
            "Time Tracker Correction Log",
            "logs"
        );
        Object.entries(source).forEach(([fieldname, value]) => {
            const serverFields = [
                "doctype",
                "name",
                "parent",
                "parentfield",
                "parenttype",
                "idx",
            ];
            if (!serverFields.includes(fieldname)) {
                row[fieldname] = value;
            }
        });
    });

    frm.refresh_field("logs");
    frm.dirty();
}


function add_reload_logs_button(frm) {
    if (!frm.doc.correction_date || !requester_can_edit(frm) || frm.is_new()) {
        return;
    }

    frm.add_custom_button(
        __("Reload Tracker Logs"),
        () => load_tracker_logs_for_date(frm, { showMessage: true }),
        __("Actions")
    );
}


function requester_can_edit(frm) {
    const state = String(frm.doc.workflow_state || "Requested");
    return Boolean(
        frm.is_new()
        || (
            state === "Requested"
            && frm.doc.requested_by === frappe.session.user
        )
    );
}


function update_correction_request_editability(frm) {
    const editable = requester_can_edit(frm);
    const reviewerRoles = [
        "Time Tracker Manager",
        "Time Tracker Log Editor",
        "HR Manager",
        "System Manager",
    ];
    const canReview = reviewerRoles.some((role) => frappe.user.has_role(role));

    const terminal = ["Approved", "Rejected", "Updated"].includes(
        String(frm.doc.workflow_state || "Requested")
    );
    frm.set_df_property("correction_date", "read_only", !editable);
    frm.set_df_property("manager_remarks", "read_only", !canReview || terminal);

    const grid = frm.fields_dict.logs && frm.fields_dict.logs.grid;
    if (!grid) {
        return;
    }

    grid.cannot_add_rows = !editable;
    grid.cannot_delete_rows = !editable;
    grid.wrapper.find(".grid-add-row, .grid-remove-rows, .grid-delete-row").toggle(editable);

    grid.grid_rows.forEach((gridRow) => {
        const rowLocked = !editable || Boolean(cint(gridRow.doc.correction_locked));
        CORRECTION_EDITABLE_FIELDS.forEach((fieldname) => {
            gridRow.toggle_editable(fieldname, !rowLocked);
        });
        [
            "row_type",
            "tracker_log",
            "original_start_time",
            "original_hours",
            "salary_slip",
            "applied_tracker_log",
            "application_status",
        ].forEach((fieldname) => gridRow.toggle_editable(fieldname, false));
    });

    grid.refresh();
}


function update_request_summary(frm) {
    const rows = frm.doc.logs || [];
    frm.doc.existing_log_count = rows.filter((row) => row.tracker_log).length;
    frm.doc.new_log_count = rows.filter((row) => !row.tracker_log).length;
    frm.doc.requested_total_hours = rows.reduce(
        (total, row) => total + flt(row.hours),
        0
    );
    frm.refresh_fields([
        "existing_log_count",
        "new_log_count",
        "requested_total_hours",
    ]);
}


function set_suggested_start_time(frm, newRow) {
    if (newRow.start_time) {
        return;
    }

    let latestMinutes = 9 * 60;
    (frm.doc.logs || []).forEach((row) => {
        if (row.name === newRow.name || !row.start_time) {
            return;
        }
        const startMinutes = parse_time_to_minutes(row.start_time);
        if (startMinutes === null) {
            return;
        }
        latestMinutes = Math.max(latestMinutes, startMinutes + flt(row.hours) * 60);
    });

    if (latestMinutes >= 24 * 60) {
        return;
    }

    const roundedMinutes = Math.round(latestMinutes);
    const hours = Math.floor(roundedMinutes / 60);
    const minutes = roundedMinutes % 60;
    frappe.model.set_value(
        newRow.doctype,
        newRow.name,
        "start_time",
        `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:00`
    );
}


function parse_time_to_minutes(value) {
    if (!value) {
        return null;
    }
    const parts = String(value).split(":");
    if (parts.length < 2) {
        return null;
    }
    const hours = Number(parts[0]);
    const minutes = Number(parts[1]);
    if (!Number.isFinite(hours) || !Number.isFinite(minutes)) {
        return null;
    }
    return hours * 60 + minutes;
}


async function configure_correction_context_fields(frm) {
    if (frm.__correction_context_loaded) {
        return;
    }

    try {
        const response = await frappe.call({
            method: "time_tracker.api.get_work_context_permissions",
            type: "POST",
            freeze: false,
        });
        const data = response.message || {};
        const permissions = data.permissions || {};
        const grid = frm.fields_dict.logs && frm.fields_dict.logs.grid;
        if (grid) {
            grid.update_docfield_property("project", "hidden", !permissions.project);
            grid.update_docfield_property("task", "hidden", !permissions.task);
            grid.update_docfield_property(
                "ticket",
                "hidden",
                !(permissions.ticket && data.ticket_doctype)
            );
        }
    } catch (error) {
        console.error("Unable to load correction work-context permissions", error);
    } finally {
        frm.__correction_context_loaded = true;
    }
}


function show_correction_status(frm) {
    const state = String(frm.doc.workflow_state || "Requested");
    if (state === "Updated") {
        frm.dashboard.set_headline_alert(
            __(
                "Tracker Logs updated: {0} existing log(s) updated and {1} new log(s) created.",
                [cint(frm.doc.updated_log_count), cint(frm.doc.created_log_count)]
            ),
            "green"
        );
    } else if (state === "Approved") {
        frm.dashboard.set_headline_alert(
            __("Approved. Tracker Logs are still unchanged; use the Update Tracker Log workflow action to apply the correction."),
            "orange"
        );
    } else if (state === "Requested") {
        frm.dashboard.set_headline_alert(
            __("Tracker Logs remain unchanged until this request is approved and the Update Tracker Log action is completed."),
            "blue"
        );
    }
}