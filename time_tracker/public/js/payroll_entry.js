frappe.ui.form.on("Payroll Entry", {
    setup(frm) {
        configure_time_tracker_employee_query(frm);
    },

    async onload(frm) {
        await load_time_tracker_payroll_setting(frm);
        configure_time_tracker_employee_query(frm);
        update_time_tracker_payroll_ui(frm);
    },

    async refresh(frm) {
        await load_time_tracker_payroll_setting(frm);
        configure_time_tracker_employee_query(frm);
        update_time_tracker_payroll_ui(frm);
        add_time_tracker_diagnostics_button(frm);
        add_time_tracker_relink_button(frm);
    },

    async company(frm) {
        frm.__bw_time_tracker_setting_company = null;
        await load_time_tracker_payroll_setting(frm);
        configure_time_tracker_employee_query(frm);
        update_time_tracker_payroll_ui(frm);
    },

    async custom_pay_using_time_tracker(frm) {
        if (
            frm.doc.custom_pay_using_time_tracker
            && frm.doc.salary_slip_based_on_timesheet
        ) {
            await frm.set_value("salary_slip_based_on_timesheet", 0);
        }

        clear_payroll_employees(frm);
        configure_time_tracker_employee_query(frm);
        update_time_tracker_payroll_ui(frm);
    },

    async salary_slip_based_on_timesheet(frm) {
        if (
            frm.doc.salary_slip_based_on_timesheet
            && frm.doc.custom_pay_using_time_tracker
        ) {
            await frm.set_value("custom_pay_using_time_tracker", 0);
        }

        clear_payroll_employees(frm);
        configure_time_tracker_employee_query(frm);
        update_time_tracker_payroll_ui(frm);
    },
});


var BW_PAYROLL_INTRO_ATTRIBUTE = "data-time_tracker-intro";
var BW_PAYROLL_INTRO_KEY = "time_tracker-time-tracker-payroll";


function uses_time_tracker_payroll(frm) {
    return Boolean(frm.doc.custom_pay_using_time_tracker);
}


async function load_time_tracker_payroll_setting(frm) {
    const company = String(frm.doc.company || "");
    if (frm.__bw_time_tracker_setting_company === company) {
        return;
    }

    frm.__bw_time_tracker_setting_company = company;
    frm.__bw_show_pay_using_time_tracker = true;

    if (!company) {
        return;
    }

    try {
        const response = await frappe.call({
            method: "time_tracker.settings.get_payroll_entry_ui_settings",
            type: "POST",
            args: { company },
        });
        const values = response.message || {};
        frm.__bw_show_pay_using_time_tracker = (
            values.show_pay_using_time_tracker !== false
        );
    } catch (error) {
        // Keep the field visible when settings cannot be loaded so an existing
        // Payroll Entry is never made impossible to review or correct.
        frm.__bw_show_pay_using_time_tracker = true;
        console.error("Unable to load Time Tracker payroll display setting", error);
    }
}


function configure_time_tracker_employee_query(frm) {
    if (!frm.fields_dict.employees) {
        return;
    }

    frm.set_query("employee", "employees", () => {
        const mandatoryFields = [
            "company",
            "payroll_frequency",
            "start_date",
            "end_date",
        ];
        const missingFields = mandatoryFields
            .filter((fieldname) => !frm.doc[fieldname])
            .map((fieldname) => frappe.unscrub(fieldname));

        if (missingFields.length) {
            frappe.throw({
                message: __(
                    "Mandatory fields required in {0}",
                    [__(frm.doc.doctype)]
                ) + `<br><br><ul><li>${missingFields.join("</li><li>")}</li></ul>`,
                indicator: "red",
                title: __("Missing Fields"),
            });
        }

        const filters = (
            frm.events
            && typeof frm.events.get_employee_filters === "function"
        )
            ? { ...frm.events.get_employee_filters(frm) }
            : {};

        if (uses_time_tracker_payroll(frm)) {
            // Time Tracker Salary Structures keep ERPNext's standard
            // Timesheet flag clear. The server query applies Based on Time
            // Tracker after HRMS returns normal non-Timesheet candidates.
            filters.salary_slip_based_on_timesheet = 0;

            return {
                query: "time_tracker.payroll.time_tracker_employee_query",
                filters,
            };
        }

        return {
            query: "hrms.payroll.doctype.payroll_entry.payroll_entry.employee_query",
            filters,
        };
    });
}


function clear_payroll_employees(frm) {
    if (frm.doc.docstatus !== 0 || !(frm.doc.employees || []).length) {
        return;
    }

    frm.clear_table("employees");
    frm.refresh_field("employees");
    frm.dirty();
}


function update_time_tracker_payroll_ui(frm) {
    const field = frm.fields_dict.custom_pay_using_time_tracker;

    if (!field) {
        return;
    }

    const usesTracker = uses_time_tracker_payroll(frm);
    const usesTimesheets = Boolean(frm.doc.salary_slip_based_on_timesheet);
    const showSelector = (
        frm.__bw_show_pay_using_time_tracker !== false
        || usesTracker
    );

    frm.toggle_display("custom_pay_using_time_tracker", showSelector);

    // Hide the selector used by an earlier development revision. Migration
    // copies its Time Tracker value to the independent checkbox and removes it.
    if (frm.fields_dict.custom_time_tracking_source) {
        frm.set_df_property("custom_time_tracking_source", "hidden", 1);
    }

    frm.set_df_property(
        "custom_pay_using_time_tracker",
        "description",
        usesTracker
            ? __(
                "Generated Salary Slips use stopped, positive, unpaid Tracker "
                + "Logs from each Employee's permanent Time Tracker. Draft and "
                + "submitted slips reserve the exact logs."
            )
            : __(
                "Enable this to pay from Time Tracker. It is separate from the "
                + "standard Salary Slip Based on Timesheet option."
            )
    );

    // Payroll Frequency remains required in Time Tracker mode because it is
    // used to define the Payroll Entry period and by HRMS's employee query.
    frm.toggle_reqd("payroll_frequency", usesTracker || !usesTimesheets);

    if (usesTracker && usesTimesheets) {
        set_time_tracker_payroll_intro(
            frm,
            BW_PAYROLL_INTRO_KEY,
            __("Select either Time Tracker payroll or Timesheet payroll, not both."),
            "red"
        );
    } else if (usesTracker) {
        set_time_tracker_payroll_intro(
            frm,
            BW_PAYROLL_INTRO_KEY,
            __(
                "Time Tracker payroll is enabled. Salary Slips will be generated "
                + "from stopped Tracker Logs."
            ),
            "blue"
        );
    } else {
        set_time_tracker_payroll_intro(frm, BW_PAYROLL_INTRO_KEY, "");
    }
}


function set_time_tracker_payroll_intro(frm, key, message, color) {
    // Frappe v15/v16 appends a new headline alert every time set_intro() is
    // called. onload and refresh can overlap, so debounce the update and own
    // only the alert tagged by this app instead of clearing other apps' alerts.
    const timerField = `__bw_intro_timer_${key.replace(/[^a-z0-9_]/gi, "_")}`;
    if (frm[timerField]) {
        window.clearTimeout(frm[timerField]);
    }

    frm[timerField] = window.setTimeout(() => {
        frm[timerField] = null;

        const $headline = frm.dashboard && frm.dashboard.headline;
        if (!$headline || !$headline.length) {
            if (message) {
                frm.set_intro(message, color);
            }
            return;
        }

        const selector = `.alert[${BW_PAYROLL_INTRO_ATTRIBUTE}="${key}"]`;
        $headline.find(selector).remove();

        if (!message) {
            if (!$headline.children().length) {
                $headline.hide();
            }
            return;
        }

        const normalisedMessage = normalise_payroll_intro_text(message);
        $headline.find(".alert").filter(function () {
            return normalise_payroll_intro_text($(this).html()) === normalisedMessage;
        }).remove();

        if (typeof frm.dashboard.set_headline_alert === "function") {
            frm.dashboard.set_headline_alert(message, color);
        } else {
            frm.set_intro(message, color);
        }
        const $currentAlert = $headline.find(".alert").last();
        $currentAlert.attr(BW_PAYROLL_INTRO_ATTRIBUTE, key);

        // A second refresh can finish after the first one. Remove any same-text
        // alert that raced with this one, while preserving unrelated messages.
        $headline.find(".alert").not($currentAlert).filter(function () {
            return normalise_payroll_intro_text($(this).html()) === normalisedMessage;
        }).remove();
        $headline.show();
    }, 0);
}


function normalise_payroll_intro_text(value) {
    return $("<div>").html(value || "").text().replace(/\s+/g, " ").trim();
}


function add_time_tracker_diagnostics_button(frm) {
    if (!uses_time_tracker_payroll(frm)) {
        return;
    }

    frm.add_custom_button(
        __("Check Time Tracker Setup"),
        async () => {
            const response = await frm.call("get_time_tracker_diagnostics");
            const diagnostics = response.message || {};
            frappe.msgprint({
                title: diagnostics.ok
                    ? __("Time Tracker Payroll Ready")
                    : __("Time Tracker Payroll Setup"),
                message: diagnostics.html || __("No diagnostic result was returned."),
                indicator: diagnostics.ok ? "green" : "orange",
                wide: true,
            });
        },
        __("Actions")
    );
}


function add_time_tracker_relink_button(frm) {
    if (
        !uses_time_tracker_payroll(frm)
        || frm.is_new()
        || frm.doc.docstatus !== 1
    ) {
        return;
    }

    frm.add_custom_button(
        __("Repair Tracker Log Links"),
        async () => {
            const response = await frm.call("relink_time_tracker_logs");
            const result = response.message || {};
            const errors = result.errors || [];
            const summary = [
                __("Salary Slips checked: {0}", [result.salary_slips || 0]),
                __("Salary Slips reconciled: {0}", [result.reconciled || 0]),
                __("Salary Slips skipped: {0}", [result.skipped || 0]),
                __("Tracker Logs linked: {0}", [result.linked_tracker_logs || 0]),
                __("Tracked hours linked: {0}", [flt(result.linked_tracker_hours || 0, 6)]),
                __("Errors: {0}", [errors.length]),
            ].join("<br>");
            const errorRows = errors.slice(0, 10).map((error) =>
                `<li><strong>${time_tracker_payroll_escape_html(error.salary_slip || "")}</strong>: `
                + `${time_tracker_payroll_escape_html(error.message || "")}</li>`
            ).join("");
            const errorDetails = errorRows
                ? `<hr><strong>${__("Details")}</strong><ul>${errorRows}</ul>`
                    + (errors.length > 10
                        ? `<p>${__("Only the first 10 errors are shown. Review Error Log for the rest.")}</p>`
                        : "")
                : "";

            frappe.msgprint({
                title: errors.length
                    ? __("Tracker Log Link Repair Completed with Errors")
                    : __("Tracker Log Links Repaired"),
                message: summary + errorDetails,
                indicator: errors.length ? "orange" : "green",
                wide: Boolean(errors.length),
            });
            frm.reload_doc();
        },
        __("Actions")
    );
}


function time_tracker_payroll_escape_html(value) {
    return $("<div>").text(value == null ? "" : String(value)).html();
}
