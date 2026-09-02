frappe.ui.form.on("Salary Slip", {
    onload(frm) {
        update_time_tracker_salary_slip_ui(frm);
    },

    refresh(frm) {
        update_time_tracker_salary_slip_ui(frm);
    },

    custom_time_tracking_source(frm) {
        update_time_tracker_salary_slip_ui(frm);
    },

    custom_payroll_hours_source(frm) {
        update_time_tracker_salary_slip_ui(frm);
    },
});


var BW_SALARY_SLIP_INTRO_ATTRIBUTE = "data-time_tracker-intro";
var BW_SALARY_SLIP_INTRO_KEY = "time_tracker-time-tracker-salary-slip";


function update_time_tracker_salary_slip_ui(frm) {
    const payrollMode = String(frm.doc.custom_time_tracking_source || "");
    const usesTrackerPayroll = payrollMode === "Time Tracker";

    // Time Tracker uses HRMS's hourly Salary Structure mechanism internally, but
    // the hours always come from Tracker Log. Hide the standard Timesheet table
    // so the document cannot appear to depend on ERPNext Timesheets.
    if (frm.fields_dict.timesheets) {
        frm.toggle_display("timesheets", !usesTrackerPayroll);
    }
    if (frm.fields_dict.custom_timesheet) {
        frm.toggle_display("custom_timesheet", false);
    }

    if (!usesTrackerPayroll) {
        set_time_tracker_salary_slip_intro(frm, BW_SALARY_SLIP_INTRO_KEY, "");
        return;
    }

    const hoursSource = String(frm.doc.custom_payroll_hours_source || "");
    if (hoursSource === "Draft Timesheet" && cint(frm.doc.docstatus) === 1) {
        // Preserve an accurate explanation for submitted slips created by the
        // temporary 0.4.1 build. Migration never rewrites submitted pay.
        set_time_tracker_salary_slip_intro(
            frm,
            BW_SALARY_SLIP_INTRO_KEY,
            __(
                "Historical Time Tracker 0.4.1 Salary Slip: its saved hours came "
                + "from Draft Timesheet rows. New Time Tracker payroll uses "
                + "Tracker Logs only."
            ),
            "orange"
        );
        return;
    }

    const trackedHours = flt(frm.doc.custom_time_tracker_hours || 0, 3);
    const payableHours = flt(
        frm.doc.custom_total_monthly_hours || frm.doc.total_working_hours || 0,
        3
    );
    const exceededHours = flt(frm.doc.custom_total_exceeded_hours || 0, 3);
    const count = cint(frm.doc.custom_time_tracker_log_count || 0);
    const tracker = frm.doc.custom_time_tracker || __("Permanent Time Tracker");

    set_time_tracker_salary_slip_intro(
        frm,
        BW_SALARY_SLIP_INTRO_KEY,
        __(
            "Paid from {0}: {1} stopped Tracker Log row(s), {2} tracked hour(s), "
            + "{3} payable hour(s), and {4} exceeded hour(s).",
            [tracker, count, trackedHours, payableHours, exceededHours]
        ),
        "blue"
    );
}


function set_time_tracker_salary_slip_intro(frm, key, message, color) {
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

        const selector = `.alert[${BW_SALARY_SLIP_INTRO_ATTRIBUTE}="${key}"]`;
        $headline.find(selector).remove();

        if (!message) {
            if (!$headline.children().length) {
                $headline.hide();
            }
            return;
        }

        const normalisedMessage = normalise_salary_slip_intro_text(message);
        $headline.find(".alert").filter(function () {
            return normalise_salary_slip_intro_text($(this).html()) === normalisedMessage;
        }).remove();

        if (typeof frm.dashboard.set_headline_alert === "function") {
            frm.dashboard.set_headline_alert(message, color);
        } else {
            frm.set_intro(message, color);
        }
        const $currentAlert = $headline.find(".alert").last();
        $currentAlert.attr(BW_SALARY_SLIP_INTRO_ATTRIBUTE, key);
        $headline.find(".alert").not($currentAlert).filter(function () {
            return normalise_salary_slip_intro_text($(this).html()) === normalisedMessage;
        }).remove();
        $headline.show();
    }, 0);
}


function normalise_salary_slip_intro_text(value) {
    return $("<div>").html(value || "").text().replace(/\s+/g, " ").trim();
}
