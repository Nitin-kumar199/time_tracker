const BW_TIME_TRACKER_CSS = "/assets/time_tracker/css/time_tracker_form.css";
const BW_TIME_TRACKER_ASSET_VERSION = "20260811-1";
const BW_TIMER_INTERVAL_MS = 1000;
const BW_RECENT_CONTEXT_LIMIT = 3;
const BW_RECENT_LOG_PAGE_LENGTH = 10;
const BW_TIMER_WIDGET_SYNC_EVENT = "time_tracker:timer-state-changed";
const BW_TIMER_WIDGET_SYNC_DEBOUNCE_MS = 120;
const BW_SALARY_SLIP_PRINT_FORMAT = "Time Tracker Salary Slip";

window.time_trackerTimeTrackerDashboardBuild = "0.6.0";

frappe.ui.form.on("Time Tracker", {
    setup(frm) {
        ensure_timer_widget_dashboard_sync();
        frm.set_df_property("employee", "read_only", !is_system_manager_client());
        frm.set_query("employee", () => ({
            filters: {
                status: "Active",
            },
        }));
    },

    async onload(frm) {
        if (frm.is_new() && !frm.doc.employee) {
            try {
                const response = await frappe.call({
                    method: "time_tracker.api.get_my_employee",
                    type: "POST",
                });

                if (response.message) {
                    await frm.set_value("employee", response.message);
                }
            } catch (error) {
                console.error("Unable to load current Employee", error);
            }
        }
    },

    async refresh(frm) {
        ensure_timer_widget_dashboard_sync();
        clear_stopwatch(frm);
        close_context_modal(frm);
        initialise_offsets(frm);
        prepare_form_shell(frm);
        frm.clear_custom_buttons();

        if (frm.is_new()) {
            render_new_tracker_state(frm);
            return;
        }

        if (!frm.doc.employee) {
            render_missing_employee_state(frm);
            return;
        }

        render_loading_state(frm);

        try {
            await load_dashboard_data(frm);
            render_dashboard(frm);
        } catch (error) {
            console.error("Unable to load Time Tracker dashboard", error);
            render_error_state(frm);
        }
    },

    before_load(frm) {
        clear_stopwatch(frm);
        close_context_modal(frm);
        destroy_recent_log_date_controls(frm);
    },
});


function is_system_manager_client() {
    return (
        frappe.session.user === "Administrator" ||
        (frappe.user_roles || []).includes("System Manager")
    );
}


/* =========================================================
   FORM SHELL
========================================================= */

function prepare_form_shell(frm) {
    load_time_tracker_styles();
    frm.set_df_property("employee", "read_only", !is_system_manager_client());

    frm.page.wrapper.addClass("bw-time-tracker-page");

    const hideNativeHeader = !frm.is_new();
    frm.page.wrapper.toggleClass("bw-hide-native-header", hideNativeHeader);
    frm.page.wrapper.find(".page-head").toggle(!hideNativeHeader);

    frm.page.wrapper
        .find(".layout-side-section")
        .hide();

    frm.page.wrapper
        .find(".layout-main-section")
        .css({
            width: "100%",
            "max-width": "100%",
        });

    frm.page.wrapper
        .find(".form-footer, .form-comments, .timeline, .new-timeline")
        .hide();

    frm.toggle_display("log_viewer", false);
    frm.toggle_display("employee", frm.is_new());
    frm.toggle_display("status", false);

    const dashboardField = frm.fields_dict.analytics_viewer;

    if (dashboardField && dashboardField.$wrapper) {
        dashboardField.$wrapper
            .closest(".frappe-control")
            .addClass("bw-dashboard-control");

        dashboardField.$wrapper
            .closest(".form-section")
            .addClass("bw-dashboard-section");
    }

    if (frm.is_new()) {
        frm.page.set_title(__("Create Time Tracker"));
    } else {
        frm.page.set_title(__("Time Tracker"));
    }
}


function load_time_tracker_styles() {
    const assetUrl = `${BW_TIME_TRACKER_CSS}?v=${encodeURIComponent(BW_TIME_TRACKER_ASSET_VERSION)}`;
    let link = document.getElementById("time_tracker-time-tracker-css");

    if (link && link.getAttribute("href") === assetUrl) {
        return;
    }

    if (link) {
        link.remove();
    }

    link = document.createElement("link");
    link.id = "time_tracker-time-tracker-css";
    link.rel = "stylesheet";
    link.href = assetUrl;

    link.onload = () => {
        document.body.classList.add("bw-time-tracker-css-ready");
    };

    link.onerror = () => {
        console.error("Unable to load Time Tracker stylesheet", assetUrl);
        frappe.show_alert({
            message: __("Time Tracker styles could not be loaded. Run bench build and clear cache."),
            indicator: "orange",
        });
    };

    document.head.appendChild(link);
}


function dashboard_wrapper(frm) {
    return frm.fields_dict.analytics_viewer.$wrapper;
}


/* =========================================================
   DASHBOARD DATA
========================================================= */

async function load_dashboard_data(frm) {
    const response = await frappe.call({
        method: "time_tracker.api.get_dashboard_data",
        type: "POST",
        args: {
            tracker: frm.doc.name,
            day_offset: frm.day_offset || 0,
            week_offset: frm.week_offset || 0,
            month_offset: frm.month_offset || 0,
            recent_log_from_date: frm.__bw_recent_log_from_date || null,
            recent_log_to_date: frm.__bw_recent_log_to_date || null,
        },
        freeze: false,
    });

    if (!response.message) {
        throw new Error("Dashboard API returned no data.");
    }

    frm.dashboard_data = response.message;

    if (response.message.offsets) {
        frm.day_offset = Number(response.message.offsets.day) || 0;
        frm.week_offset = Number(response.message.offsets.week) || 0;
        frm.month_offset = Number(response.message.offsets.month) || 0;
    }

    return response.message;
}


async function reload_dashboard(frm, { silent = false } = {}) {
    clear_stopwatch(frm);

    const wrapper = dashboard_wrapper(frm);
    const pageScrollTop = window.scrollY || document.documentElement.scrollTop || 0;
    const activityScrollTop = wrapper.find(".bw-activity-list").scrollTop() || 0;

    wrapper.attr("aria-busy", "true");

    try {
        await load_dashboard_data(frm);
        render_dashboard(frm);

        window.requestAnimationFrame(() => {
            window.scrollTo({ top: pageScrollTop, left: 0, behavior: "auto" });
            dashboard_wrapper(frm).find(".bw-activity-list").scrollTop(activityScrollTop);
        });
    } catch (error) {
        console.error("Unable to reload Time Tracker dashboard", error);

        if (!silent) {
            frappe.show_alert({
                message: __("The dashboard could not be refreshed."),
                indicator: "red",
            });
        }

        wrapper.attr("aria-busy", "false");
    }
}


function initialise_offsets(frm) {
    if (frm.day_offset === undefined) {
        frm.day_offset = 0;
    }

    if (frm.week_offset === undefined) {
        frm.week_offset = 0;
    }

    if (frm.month_offset === undefined) {
        frm.month_offset = 0;
    }
}


/* =========================================================
   STATES
========================================================= */

function render_loading_state(frm) {
    destroy_recent_log_date_controls(frm);
    dashboard_wrapper(frm).html(`
        <div class="bw-time-app" aria-busy="true">
            <div class="bw-skeleton bw-skeleton-heading"></div>
            <div class="bw-skeleton-grid">
                <div class="bw-skeleton bw-skeleton-hero"></div>
                <div class="bw-skeleton bw-skeleton-panel"></div>
            </div>
            <div class="bw-skeleton-kpis">
                ${Array.from({ length: 4 }, () => `
                    <div class="bw-skeleton bw-skeleton-kpi"></div>
                `).join("")}
            </div>
        </div>
    `);
}


function render_new_tracker_state(frm) {
    destroy_recent_log_date_controls(frm);
    const wrapper = dashboard_wrapper(frm);

    wrapper.html(`
        <div class="bw-time-app">
            <div class="bw-state-card">
                <div class="bw-state-icon">◷</div>
                <h2>${__("Create your Time Tracker")}</h2>
                <p>
                    ${__(
                        "Confirm the Employee and save this document once. " +
                        "After that, this same record becomes the employee's personal time dashboard."
                    )}
                </p>
            </div>
        </div>
    `);
}


function render_missing_employee_state(frm) {
    destroy_recent_log_date_controls(frm);
    dashboard_wrapper(frm).html(`
        <div class="bw-time-app">
            <div class="bw-state-card bw-state-card-warning">
                <div class="bw-state-icon">!</div>
                <h2>${__("Employee is missing")}</h2>
                <p>${__("Link an active Employee to use this Time Tracker.")}</p>
            </div>
        </div>
    `);
}


function render_error_state(frm) {
    destroy_recent_log_date_controls(frm);
    const wrapper = dashboard_wrapper(frm);

    wrapper.html(`
        <div class="bw-time-app">
            <div class="bw-state-card bw-state-card-error">
                <div class="bw-state-icon">!</div>
                <h2>${__("We could not load your time dashboard")}</h2>
                <p>${__("Your timer data was not changed. Try loading the dashboard again.")}</p>
                <button type="button" class="btn btn-primary" data-bw-action="retry">
                    ${__("Try again")}
                </button>
            </div>
        </div>
    `);

    wrapper
        .find('[data-bw-action="retry"]')
        .off("click")
        .on("click", async () => {
            render_loading_state(frm);

            try {
                await load_dashboard_data(frm);
                render_dashboard(frm);
            } catch (error) {
                console.error("Unable to retry Time Tracker dashboard", error);
                render_error_state(frm);
            }
        });
}


/* =========================================================
   MAIN RENDER
========================================================= */

function render_dashboard(frm) {
    destroy_recent_log_date_controls(frm);

    const data = frm.dashboard_data || {};
    const profile = data.profile || {};
    const stats = data.stats || {};
    const daily = stats.daily || {};
    const weekly = stats.weekly || {};
    const monthly = stats.monthly || {};
    const runningLog = data.running_log || null;
    const canControl = Boolean(data.can_control);
    const contextPermissions = data.context_permissions || {};
    const canSelectContext = Boolean(
        contextPermissions.project || contextPermissions.ticket
    );
    const contexts = recent_contexts(data.recent_logs || [], runningLog);
    const recentActivityLogs = data.recent_activity_logs || data.recent_logs || [];
    const employeeName = first_and_last_name(
        profile.employee_name || profile.employee || ""
    );
    const isRunning = Boolean(runningLog);
    const showSalarySlips = data.show_salary_slips !== false;
    const salaryPermissions = showSalarySlips
        ? (data.salary_slip_permissions || {})
        : {};
    const browserWidgetSchemaReady = data.browser_widget_schema_ready !== false;
    const browserWidgetEnabled = Boolean(data.browser_widget_enabled);
    const canManageBrowserWidget = Boolean(
        data.can_manage_browser_widget || canControl
    );

    frm.__bw_quick_contexts = contexts;
    frm.__bw_recent_logs = [...recentActivityLogs];
    frm.__bw_recent_logs_has_more = Boolean(
        data.recent_activity_logs_has_more ?? data.recent_logs_has_more
    );
    frm.__bw_recent_log_from_date = String(data.recent_log_from_date || "");
    frm.__bw_recent_log_to_date = String(data.recent_log_to_date || "");
    frm.__bw_recent_log_page_length = Number(data.recent_logs_page_length)
        || BW_RECENT_LOG_PAGE_LENGTH;
    frm.__bw_salary_slips = showSalarySlips ? [...(data.salary_slips || [])] : [];
    frm.__bw_salary_slip_permissions = salaryPermissions;
    frm.__bw_show_salary_slips = showSalarySlips;
    frm.__bw_salary_slip_print_format = String(
        data.salary_slip_print_format || BW_SALARY_SLIP_PRINT_FORMAT
    );

    if (!frm.__bw_records_tab) {
        frm.__bw_records_tab = "activity";
    }
    if ((!showSalarySlips || !salaryPermissions.read) && frm.__bw_records_tab === "salary-slips") {
        frm.__bw_records_tab = "activity";
    }

    if (canControl) {
        frm.page.set_title(__("Time Tracker"));
    } else {
        frm.page.set_title(
            __("{0}'s Time Tracker", [profile.employee_name || profile.employee || __("Employee")])
        );
    }

    const html = `
        <div class="bw-time-app" aria-busy="false">
            ${!canControl ? read_only_banner(profile) : ""}

            <section class="bw-greeting">
                <div>
                    <h1>${escape_html(greeting_text(employeeName))}</h1>
                    <p>${escape_html(greeting_support_text({ weekly, isRunning, canControl }))}</p>
                </div>
                <div class="bw-date-chip" aria-label="${escape_attr(format_long_date(data.today))}">
                    <span class="bw-date-chip-icon" aria-hidden="true">◷</span>
                    <span class="bw-date-chip-copy">
                        <strong>${escape_html(moment(data.today, "YYYY-MM-DD").format("dddd"))}</strong>
                        <small>${escape_html(moment(data.today, "YYYY-MM-DD").format("D MMMM YYYY"))}</small>
                    </span>
                </div>
            </section>

            <section class="bw-hero-grid">
                ${timer_hero({
                    data,
                    runningLog,
                    canControl,
                    canSelectContext,
                    browserWidgetSchemaReady,
                    browserWidgetEnabled,
                    canManageBrowserWidget,
                })}
                ${employee_details_panel(profile)}
            </section>

            <section class="bw-kpi-grid" aria-label="${__("Time summary")}">
                ${kpi_card({
                    title: __("Today"),
                    value: format_duration(Number(daily.hours) || 0),
                    valueClass: "bw-today-value",
                    foot: format_short_date(daily.date),
                    icon: "◷",
                })}

                ${kpi_card({
                    title: frm.week_offset === 0 ? __("This week") : __("Selected week"),
                    value: format_duration(Number(weekly.hours) || 0),
                    valueClass: "bw-week-value",
                    foot: weekly_goal_copy(weekly),
                    footClass: "bw-week-goal-copy",
                    navigation: kpi_period_navigation({
                        label: format_week_range(weekly.start_date, weekly.end_date),
                        navigationLabel: __("Week navigation"),
                        previousAction: "previous-week",
                        previousLabel: __("Previous week"),
                        nextAction: "next-week",
                        nextLabel: __("Next week"),
                        canMoveForward: (frm.week_offset || 0) < 0,
                    }),
                })}

                ${kpi_card({
                    title: frm.month_offset === 0 ? __("This month") : __("Selected month"),
                    value: format_duration(Number(monthly.hours) || 0),
                    valueClass: "bw-month-value",
                    foot: monthly_goal_copy(monthly),
                    footClass: "bw-month-goal-copy",
                    navigation: kpi_period_navigation({
                        label: format_month_label(monthly.start_date),
                        navigationLabel: __("Month navigation"),
                        previousAction: "previous-month",
                        previousLabel: __("Previous month"),
                        nextAction: "next-month",
                        nextLabel: __("Next month"),
                        canMoveForward: (frm.month_offset || 0) < 0,
                    }),
                })}

                ${kpi_card({
                    title: __("Exceeded hours"),
                    value: format_duration(Number(weekly.exceeded_hours) || 0),
                    valueClass: "bw-week-exceeded-value",
                    foot: weekly_exceeded_copy(weekly),
                    footClass: "bw-week-exceeded-copy",
                    icon: "↗",
                })}
            </section>

            <section class="bw-weekly-section">
                ${weekly_rhythm_card(data)}
            </section>

            <section class="bw-heatmap-section">
                ${consistency_card(frm, data)}
            </section>

            ${records_tabs_section({
                logs: frm.__bw_recent_logs,
                hasMore: frm.__bw_recent_logs_has_more,
                fromDate: frm.__bw_recent_log_from_date,
                toDate: frm.__bw_recent_log_to_date,
                salarySlips: frm.__bw_salary_slips,
                salaryPermissions,
                activeTab: frm.__bw_records_tab,
            })}

            ${mobile_timer_action({ runningLog, canControl, canSelectContext })}
        </div>
    `;

    const wrapper = dashboard_wrapper(frm);
    wrapper.html(html).removeClass("bw-is-refreshing").attr("aria-busy", "false");

    bind_dashboard_actions(frm, wrapper);
    render_browser_widget_toolbar_button(frm);

    if (runningLog) {
        start_stopwatch(frm, Number(data.running_elapsed_seconds) || 0);
    }
}


function timer_hero({
    data,
    runningLog,
    canControl,
    canSelectContext,
    browserWidgetSchemaReady,
    browserWidgetEnabled,
    canManageBrowserWidget,
}) {
    const isRunning = Boolean(runningLog);
    const context = context_details(runningLog || {});
    const statusText = isRunning ? __("Session in progress") : __("Ready to focus");
    const timerValue = isRunning
        ? format_clock_seconds(Number(data.running_elapsed_seconds) || 0)
        : "00:00:00";

    const workLabel = isRunning ? __("Working on") : __("Start your next session");
    const workTitle = isRunning
        ? context.title
        : __("Choose a project and task.");

    let actions = "";

    if (canControl && isRunning) {
        actions = `
            <button type="button" class="bw-hero-primary" data-bw-action="stop">
                <span aria-hidden="true">■</span>
                ${__("Stop session")}
            </button>
            <button type="button" class="bw-hero-secondary" data-bw-action="switch">
                ${__("Switch task")}
            </button>
        `;
    } else if (canControl) {
        actions = `
            <button
                type="button"
                class="bw-hero-primary bw-hero-primary-start"
                data-bw-action="start"
                ${canSelectContext ? "" : "disabled"}
                title="${escape_attr(
                    canSelectContext
                        ? __("Start a session")
                        : __("You need read access to Project, Task, or Ticket before starting a session.")
                )}"
            >
                <span aria-hidden="true">▶</span>
                ${__("Start session")}
            </button>
        `;
    }

    return `
        <article class="bw-timer-hero ${isRunning ? "bw-is-running" : "bw-is-idle"}">
            <div class="bw-hero-top">
                <div class="bw-live-pill">
                    <span class="bw-live-dot" aria-hidden="true"></span>
                    ${escape_html(statusText)}
                </div>
                ${browser_widget_preference({
                    schemaReady: browserWidgetSchemaReady,
                    enabled: browserWidgetEnabled,
                    canManage: canManageBrowserWidget,
                })}
            </div>

            <div class="bw-timer-value" aria-live="polite">${escape_html(timerValue)}</div>

            <div class="bw-work-label">${escape_html(workLabel)}</div>
            <div class="bw-work-title">${escape_html(workTitle)}</div>
            ${context.meta && isRunning ? `<div class="bw-work-meta">${escape_html(context.meta)}</div>` : ""}

            ${actions ? `
                <div class="bw-hero-actions ${!isRunning ? "bw-hero-actions-single" : ""}">
                    ${actions}
                </div>
            ` : ""}
        </article>
    `;
}


function render_browser_widget_toolbar_button(frm) {
    const enableLabel = __("Enable Widget");
    const disableLabel = __("Disable Widget");

    if (typeof frm.remove_custom_button === "function") {
        frm.remove_custom_button(enableLabel);
        frm.remove_custom_button(disableLabel);
    }

    const data = frm.dashboard_data || {};
    if (!(data.can_manage_browser_widget || data.can_control)) {
        return;
    }

    if (data.browser_widget_schema_ready === false) {
        const button = frm.add_custom_button(
            enableLabel,
            show_browser_widget_migration_required
        );
        button.attr({
            "aria-disabled": "true",
            title: __("Run bench migrate to install the Browser Widget preference field."),
        });
        return;
    }

    const enabled = Boolean(data.browser_widget_enabled);
    const nextEnabled = !enabled;
    const label = enabled ? disableLabel : enableLabel;
    let button = null;

    const applyPreference = async () => {
        await update_browser_widget_preference(frm, nextEnabled, button);
    };

    button = frm.add_custom_button(label, () => {
        const running = Boolean((frm.dashboard_data || {}).running_log);
        if (!nextEnabled && running) {
            frappe.confirm(
                __(
                    "Disabling the Browser Widget only hides the floating timer. "
                    + "Your current session will keep running until you stop it here. Continue?"
                ),
                applyPreference
            );
            return;
        }

        applyPreference();
    });

    button.attr("data-time_tracker-browser-widget-button", "1");
    if (!enabled) {
        button.addClass("btn-primary");
    }
}


function browser_widget_preference({ schemaReady, enabled, canManage }) {
    if (!canManage) {
        return "";
    }

    if (!schemaReady) {
        return `
            <button
                type="button"
                class="bw-browser-widget-toggle is-unavailable"
                data-bw-action="browser-widget-migration-help"
                aria-disabled="true"
                title="${escape_attr(__("Run bench migrate to install the Browser Widget preference field."))}"
            >
                <span class="bw-browser-widget-dot" aria-hidden="true"></span>
                <span>${escape_html(__("Enable browser widget"))}</span>
            </button>
        `;
    }

    const label = enabled
        ? __("Disable Widget")
        : __("Enable Widget");
    const title = enabled
        ? __("Disable the floating timer on Frappe Desk")
        : __("Show the floating timer on Frappe Desk");

    return `
        <button
            type="button"
            class="bw-browser-widget-toggle ${enabled ? "is-enabled" : ""}"
            data-bw-action="toggle-browser-widget"
            data-bw-enabled="${enabled ? "1" : "0"}"
            aria-pressed="${enabled ? "true" : "false"}"
            title="${escape_attr(title)}"
        >
            <span class="bw-browser-widget-dot" aria-hidden="true"></span>
            <span>${escape_html(label)}</span>
        </button>
    `;
}


function show_browser_widget_migration_required() {
    frappe.msgprint({
        title: __("Migration required"),
        message: __(
            "The Browser Widget preference field is not installed on this site yet. "
            + "Ask an administrator to run bench migrate, rebuild Time Tracker assets, "
            + "and clear the site cache."
        ),
        indicator: "orange",
    });
}


function quick_switch_panel(contexts, isRunning) {
    const contextItems = contexts.length
        ? contexts.map((context, index) => `
            <button
                type="button"
                class="bw-quick-item"
                data-bw-context-index="${index}"
                aria-label="${escape_attr(__("Use {0}", [context.title]))}"
            >
                <span class="bw-context-icon bw-context-colour-${(index % 3) + 1}">
                    ${escape_html(context.initials)}
                </span>
                <span class="bw-context-copy">
                    <strong>${escape_html(context.title)}</strong>
                    <span>${escape_html(context.meta)}</span>
                </span>
                <span class="bw-context-arrow" aria-hidden="true">›</span>
            </button>
        `).join("")
        : `
            <div class="bw-quick-empty">
                ${__("Your recent projects, tasks, and tickets will appear here.")}
            </div>
        `;

    return `
        <aside class="bw-quick-panel">
            <div class="bw-card-heading-row">
                <div>
                    <h2>${isRunning ? __("Quick switch") : __("Quick start")}</h2>
                    <p>
                        ${isRunning
                            ? __("Stop the current session and continue in another context")
                            : __("Reuse one of your recent work contexts")}
                    </p>
                </div>
                <span class="bw-panel-mark" aria-hidden="true">✦</span>
            </div>

            <div class="bw-quick-list">${contextItems}</div>

            <button type="button" class="bw-quick-add" data-bw-action="choose-context">
                + ${__("Choose another task")}
            </button>
        </aside>
    `;
}


function employee_details_panel(profile) {
    const displayName = first_and_last_name(
        profile.employee_name || profile.employee || ""
    );

    return `
        <aside class="bw-quick-panel bw-details-panel bw-employee-panel">
            <div class="bw-card-heading-row">
                <div>
                    <h2>${__("Employee details")}</h2>
                    <p>${__("Employment and tracker availability")}</p>
                </div>
                ${employee_status_badge(profile.status)}
            </div>

            <div class="bw-employee-summary">
                <span class="bw-employee-avatar" aria-hidden="true">
                    <span class="bw-employee-avatar-text">
                        ${escape_html(person_initials(displayName || profile.employee))}
                    </span>
                </span>
                <div>
                    <strong>${escape_html(displayName || profile.employee || __("Employee"))}</strong>
                    <span>${escape_html(profile.designation || __("Designation not set"))}</span>
                </div>
            </div>

            <dl class="bw-detail-list">
                ${detail_row(__("Status"), profile.status)}
                ${detail_row(__("Company"), profile.company)}
                ${detail_row(__("Name"), displayName)}
                ${detail_row(__("Department"), profile.department)}
                ${detail_row(__("Designation"), profile.designation)}
                ${detail_row(__("Joining date"), format_employee_date(profile.date_of_joining))}
            </dl>
        </aside>
    `;
}


function employee_status_badge(status) {
    const value = String(status || __("Not set"));
    const className = {
        Active: "bw-status-active",
        Inactive: "bw-status-inactive",
        Suspended: "bw-status-suspended",
        Left: "bw-status-left",
    }[value] || "bw-status-inactive";

    return `
        <span class="bw-employee-status ${className}" title="${escape_attr(__("Employee status"))}">
            <span aria-hidden="true"></span>
            ${escape_html(value)}
        </span>
    `;
}


function detail_row(label, value) {
    return `
        <div class="bw-detail-row">
            <dt>${escape_html(label)}</dt>
            <dd>${escape_html(value || __("Not set"))}</dd>
        </div>
    `;
}


function read_only_banner(profile) {
    const employeeName = profile.employee_name || profile.employee || __("this employee");
    const employeeStatus = String(profile.status || "");
    const restriction = employeeStatus && employeeStatus !== "Active"
        ? __(
            "Employee status is {0}. Historical time remains available, but sessions cannot run until the employee is Active.",
            [employeeStatus]
        )
        : __(
            "Only the tracker owner can start or stop sessions."
        );

    return `
        <div class="bw-read-only-banner" role="status">
            <div class="bw-read-only-copy">
                <span class="bw-read-only-message">
                    <span>
                        ${escape_html(__("You can review {0}'s time tracker data.", [employeeName]))}
                    </span>
                    <strong class="bw-read-only-restriction">
                        ${escape_html(restriction)}
                    </strong>
                </span>
            </div>
        </div>
    `;
}


/* =========================================================
   KPI AND INSIGHTS
========================================================= */

function kpi_card({
    title,
    value,
    foot,
    icon = "",
    navigation = "",
    valueClass = "",
    footClass = "",
}) {
    const headControl = navigation || `
        <span class="bw-kpi-icon" aria-hidden="true">${escape_html(icon)}</span>
    `;

    return `
        <article class="bw-kpi-card ${navigation ? "bw-kpi-card-period" : ""}">
            <div class="bw-kpi-head">
                <span class="bw-kpi-title">${escape_html(title)}</span>
                ${headControl}
            </div>
            <div class="bw-kpi-value ${escape_attr(valueClass)}">${escape_html(value)}</div>
            <div class="bw-kpi-foot ${escape_attr(footClass)}">${escape_html(foot)}</div>
        </article>
    `;
}


function kpi_period_navigation({
    label,
    navigationLabel,
    previousAction,
    previousLabel,
    nextAction,
    nextLabel,
    canMoveForward,
}) {
    return `
        <div
            class="bw-kpi-period-navigation"
            role="group"
            aria-label="${escape_attr(navigationLabel)}"
        >
            <button
                type="button"
                data-bw-action="${escape_attr(previousAction)}"
                aria-label="${escape_attr(previousLabel)}"
            >‹</button>
            <span title="${escape_attr(label)}">${escape_html(label)}</span>
            <button
                type="button"
                data-bw-action="${escape_attr(nextAction)}"
                aria-label="${escape_attr(nextLabel)}"
                ${canMoveForward ? "" : "disabled"}
            >›</button>
        </div>
    `;
}


function weekly_rhythm_card(data) {
    const weekly = (data.stats || {}).weekly || {};
    const days = week_days(data);
    const maximum = Math.max(8, ...days.map((day) => day.hours));

    const bars = days.map((day) => {
        const height = day.hours > 0
            ? Math.max(5, Math.min(100, (day.hours / maximum) * 100))
            : 0;

        return `
            <div class="bw-bar-column" title="${escape_attr(
                `${format_long_date(day.date)}: ${format_duration(day.hours)}`
            )}">
                <div class="bw-bar-track">
                    <div
                        class="bw-week-bar ${day.isToday ? "bw-week-bar-today" : ""}"
                        style="--bw-bar-height: ${height.toFixed(2)}%"
                    ></div>
                </div>
                <span>${escape_html(day.label)}</span>
            </div>
        `;
    }).join("");

    const goal = Number(weekly.limit) || 0;
    const hours = Number(weekly.hours) || 0;
    const progress = goal > 0 ? Math.min(100, Math.max(0, (hours / goal) * 100)) : 0;
    const isExceeded = goal > 0 && hours > goal;

    return `
        <article class="bw-insight-card">
            <div class="bw-card-heading-row bw-chart-heading">
                <div>
                    <h2>${__("Weekly rhythm")}</h2>
                    <p>${__("Focused hours by day")}</p>
                </div>
            </div>

            <div class="bw-weekly-content">
                <div class="bw-weekly-chart-area">
                    <div class="bw-week-chart">${bars}</div>

                    <div class="bw-chart-footer">
                        <span>${escape_html(format_week_range(weekly.start_date, weekly.end_date))}</span>
                        <span>
                            <strong class="bw-week-value">${escape_html(format_duration(hours))}</strong>
                            ${goal ? ` ${__("of")} ${escape_html(format_duration(goal))}` : ""}
                        </span>
                    </div>

                    ${goal ? `
                        <div class="bw-goal-track" aria-label="${escape_attr(__("Weekly goal progress"))}">
                            <span
                                class="bw-goal-fill ${isExceeded ? "bw-goal-fill-exceeded" : ""}"
                                style="--bw-goal-progress: ${progress.toFixed(2)}%"
                            ></span>
                        </div>
                    ` : ""}
                </div>
            </div>
        </article>
    `;
}


function consistency_card(frm, data) {
    const monthly = (data.stats || {}).monthly || {};
    const selectedDate = moment(monthly.start_date, "YYYY-MM-DD", true);
    const selectedYear = selectedDate.isValid() ? selectedDate.year() : moment().year();
    const currentYear = moment(data.today, "YYYY-MM-DD", true).year();
    const canMoveForward = selectedYear < currentYear;

    return `
        <article class="bw-insight-card bw-year-heatmap-card">
            <div class="bw-card-heading-row bw-chart-heading">
                <div>
                    <h2>${__("Activity heatmap")}</h2>
                    <p>${escape_html(__("Daily activity across {0}", [selectedYear]))}</p>
                </div>
                <div class="bw-period-navigation bw-year-navigation" aria-label="${__("Year navigation")}">
                    <button type="button" data-bw-action="previous-year" aria-label="${__("Previous year")}">‹</button>
                    <span>${selectedYear}</span>
                    <button
                        type="button"
                        data-bw-action="next-year"
                        aria-label="${__("Next year")}"
                        ${canMoveForward ? "" : "disabled"}
                    >›</button>
                </div>
            </div>

            ${year_heatmap(data, selectedYear)}
        </article>
    `;
}


function year_heatmap(data, selectedYear) {
    const yearStart = moment(`${selectedYear}-01-01`, "YYYY-MM-DD", true);
    const yearEnd = moment(`${selectedYear}-12-31`, "YYYY-MM-DD", true);

    if (!yearStart.isValid() || !yearEnd.isValid()) {
        return `<div class="bw-year-heatmap-empty">${__("Heatmap data is unavailable.")}</div>`;
    }

    const gridStart = yearStart.clone().startOf("isoWeek");
    const gridEnd = yearEnd.clone().endOf("isoWeek");
    const weekCount = gridEnd.diff(gridStart, "weeks") + 1;
    const today = moment(data.today, "YYYY-MM-DD", true);
    const cells = [];
    const monthLabels = [];

    for (let month = 0; month < 12; month += 1) {
        const monthStart = yearStart.clone().month(month).startOf("month");
        const weekIndex = monthStart.clone().startOf("isoWeek").diff(gridStart, "weeks");
        monthLabels.push({
            label: monthStart.format("MMM"),
            column: Math.max(1, weekIndex + 1),
        });
    }

    for (let week = 0; week < weekCount; week += 1) {
        for (let day = 0; day < 7; day += 1) {
            const current = gridStart.clone().add(week, "weeks").add(day, "days");
            const inYear = current.year() === selectedYear;
            const isFuture = current.isAfter(today, "day");
            const dateString = current.format("YYYY-MM-DD");
            const hours = inYear && !isFuture ? hours_for_date(data, dateString) : 0;
            const level = heat_level(hours);
            const label = inYear
                ? `${format_long_date(dateString)}: ${format_duration(hours)}`
                : "";

            cells.push(`
                <span
                    class="bw-year-heat-cell ${inYear ? "" : "bw-year-heat-cell-outside"} ${isFuture ? "bw-year-heat-cell-future" : ""}"
                    data-level="${level}"
                    title="${escape_attr(label)}"
                    aria-label="${escape_attr(label || __("Outside selected year"))}"
                ></span>
            `);
        }
    }

    return `
        <div class="bw-year-heatmap-scroll" tabindex="0">
            <div class="bw-year-heatmap" style="--bw-year-weeks: ${weekCount}">
                <div class="bw-year-months" aria-hidden="true">
                    ${monthLabels.map((month) => `
                        <span style="grid-column: ${month.column}">${escape_html(month.label)}</span>
                    `).join("")}
                </div>
                <div class="bw-year-weekdays" aria-hidden="true">
                    <span style="grid-row: 1">${__("Mon")}</span>
                    <span style="grid-row: 3">${__("Wed")}</span>
                    <span style="grid-row: 5">${__("Fri")}</span>
                </div>
                <div class="bw-year-heat-grid">${cells.join("")}</div>
                <div class="bw-year-heat-legend" aria-hidden="true">
                    <span>${__("Less")}</span>
                    ${[0, 1, 2, 3, 4].map((level) => `<i data-level="${level}"></i>`).join("")}
                    <span>${__("More")}</span>
                </div>
            </div>
        </div>
    `;
}


function heat_level(hours) {
    if (hours <= 0) {
        return 0;
    }

    if (hours < 2) {
        return 1;
    }

    if (hours < 4) {
        return 2;
    }

    if (hours < 7) {
        return 3;
    }

    return 4;
}


/* =========================================================
   RECENT SESSIONS
========================================================= */

function records_tabs_section({
    logs,
    hasMore,
    fromDate,
    toDate,
    salarySlips,
    salaryPermissions,
    activeTab,
}) {
    const canReadSalarySlips = Boolean(salaryPermissions && salaryPermissions.read);
    const selectedTab = canReadSalarySlips && activeTab === "salary-slips"
        ? "salary-slips"
        : "activity";

    return `
        <section class="bw-records-section">
            <div class="bw-record-tabs" role="tablist" aria-label="${escape_attr(__("Time Tracker records"))}">
                <button
                    type="button"
                    role="tab"
                    class="bw-record-tab ${selectedTab === "activity" ? "bw-record-tab-active" : ""}"
                    data-bw-record-tab="activity"
                    aria-selected="${selectedTab === "activity" ? "true" : "false"}"
                >
                    ${__("Recent activity")}
                </button>
                ${canReadSalarySlips ? `
                    <button
                        type="button"
                        role="tab"
                        class="bw-record-tab ${selectedTab === "salary-slips" ? "bw-record-tab-active" : ""}"
                        data-bw-record-tab="salary-slips"
                        aria-selected="${selectedTab === "salary-slips" ? "true" : "false"}"
                    >
                        ${__("Salary slips")}
                    </button>
                ` : ""}
            </div>

            <div
                class="bw-record-panel"
                data-bw-record-panel="activity"
                ${selectedTab === "activity" ? "" : "hidden"}
            >
                ${recent_sessions_card(logs, hasMore, fromDate, toDate)}
            </div>

            ${canReadSalarySlips ? `
                <div
                    class="bw-record-panel"
                    data-bw-record-panel="salary-slips"
                    ${selectedTab === "salary-slips" ? "" : "hidden"}
                >
                    ${salary_slips_card(salarySlips, salaryPermissions)}
                </div>
            ` : ""}
        </section>
    `;
}


function salary_slips_card(salarySlips, permissions) {
    const rows = salarySlips.length
        ? salarySlips.map((slip) => salary_slip_row(slip, permissions)).join("")
        : `
            <div class="bw-empty-activity bw-empty-salary-slips">
                <span aria-hidden="true">¤</span>
                <strong>${__("No salary slips available")}</strong>
                <p>${__("Draft and submitted Salary Slips available to this employee will appear here.")}</p>
            </div>
        `;

    return `
        <article class="bw-insight-card bw-salary-card">
            <div class="bw-card-heading-row">
                <div>
                    <h2>${__("Salary slips")}</h2>
                    <p>${__("All draft and submitted Salary Slips available for this employee")}</p>
                </div>
            </div>
            <div class="bw-salary-list">${rows}</div>
        </article>
    `;
}


function salary_slip_row(slip, permissions) {
    const isPaid = Number(slip.docstatus) === 1;
    const status = isPaid ? __("Paid") : __("Unpaid");
    const statusClass = isPaid ? "bw-salary-status-paid" : "bw-salary-status-unpaid";
    const hours = Number(
        slip.custom_time_tracker_hours ?? slip.total_working_hours
    ) || 0;
    const period = [format_short_date(slip.start_date), format_short_date(slip.end_date)]
        .filter(Boolean)
        .join(" – ");

    return `
        <div class="bw-salary-row">
            <div class="bw-salary-main">
                <strong>${escape_html(slip.name || __("Salary Slip"))}</strong>
                <span>${escape_html(period || format_short_date(slip.posting_date))}</span>
            </div>
            <span class="bw-salary-status ${statusClass}" role="status">${escape_html(status)}</span>
            <span class="bw-salary-hours">${escape_html(format_duration(hours))}</span>
            <span class="bw-salary-amount">${escape_html(format_salary_amount(slip.net_pay, slip.currency))}</span>
            <div class="bw-salary-actions">
                <button
                    type="button"
                    class="bw-salary-action"
                    data-bw-salary-view="${escape_attr(slip.name || "")}"
                >${__("View")}</button>
                ${slip.can_print ? `
                    <button
                        type="button"
                        class="bw-salary-action"
                        data-bw-salary-download="${escape_attr(slip.name || "")}"
                    >${__("Download PDF")}</button>
                ` : ""}
            </div>
        </div>
    `;
}


function format_salary_amount(value, currency) {
    const amount = Number(value) || 0;

    try {
        const formatted = frappe.format(amount, {
            fieldtype: "Currency",
            options: currency || undefined,
        });
        const text = formatted_value_text(formatted);
        return text || `${currency || ""} ${amount.toFixed(2)}`.trim();
    } catch (error) {
        return `${currency || ""} ${amount.toFixed(2)}`.trim();
    }
}


function formatted_value_text(value) {
    const container = document.createElement("div");
    container.innerHTML = String(value ?? "");
    return String(container.textContent || container.innerText || "")
        .replace(/\s+/g, " ")
        .trim();
}


function recent_sessions_card(logs, hasMore, fromDate = "", toDate = "") {
    const hasFilters = Boolean(fromDate || toDate);
    const rows = logs.length
        ? `
            <div class="bw-activity-columns" aria-hidden="true">
                <span></span>
                <span>${__("Project / task")}</span>
                <span>${__("Date")}</span>
                <span>${__("Start – End")}</span>
                <span class="bw-activity-column-hours">${__("Hours")}</span>
            </div>
            ${recent_session_rows(logs)}
        `
        : `
            <div class="bw-empty-activity">
                <span aria-hidden="true">◷</span>
                <strong>
                    ${hasFilters ? __("No sessions match these filters") : __("No sessions yet")}
                </strong>
                <p>
                    ${hasFilters
                        ? __("Choose another date range or clear the filters.")
                        : __("Your completed work sessions will appear here.")}
                </p>
            </div>
        `;

    return `
        <article class="bw-insight-card bw-activity-card">
            <div class="bw-card-heading-row bw-activity-heading">
                <div>
                    <h2>${__("Recent sessions")}</h2>
                    <p>${__("Filter this Time Tracker's activity without changing the document")}</p>
                </div>

                <div class="bw-activity-filter" aria-label="${escape_attr(__("Filter recent sessions"))}">
                    <div class="bw-activity-filter-controls">
                        <div class="bw-activity-date-field">
                            <label for="bw-recent-session-from-date">${__("From Date")}</label>
                            <input
                                type="date"
                                id="bw-recent-session-from-date"
                                class="bw-date-input"
                                data-bw-date-input="from"
                                value="${escape_attr(fromDate || "")}"
                                aria-label="${escape_attr(__("Filter recent sessions from date"))}"
                            >
                        </div>

                        <div class="bw-activity-date-field">
                            <label for="bw-recent-session-to-date">${__("To Date")}</label>
                            <input
                                type="date"
                                id="bw-recent-session-to-date"
                                class="bw-date-input"
                                data-bw-date-input="to"
                                value="${escape_attr(toDate || "")}"
                                aria-label="${escape_attr(__("Filter recent sessions to date"))}"
                            >
                        </div>

                        <button
                            type="button"
                            class="bw-activity-filter-apply"
                            data-bw-action="apply-recent-date-range"
                        >
                            ${__("Apply")}
                        </button>

                        <button
                            type="button"
                            class="bw-activity-filter-clear"
                            data-bw-action="clear-recent-date-range"
                            ${hasFilters ? "" : "disabled"}
                        >
                            ${__("Clear")}
                        </button>
                    </div>
                </div>
            </div>

            <div class="bw-activity-list" tabindex="0">${rows}</div>

            ${hasMore ? `
                <div class="bw-activity-footer">
                    <button type="button" class="bw-load-more" data-bw-action="load-more-logs">
                        ${__("Load more")}
                    </button>
                </div>
            ` : ""}
        </article>
    `;
}


function recent_session_rows(logs, startIndex = 0) {
    return logs
        .map((log, index) => recent_session_row(log, startIndex + index))
        .join("");
}


function recent_session_row(log, index) {
    const context = context_details(log);
    const description = String(log.description || "").trim();
    const contextMeta = [context.meta, description].filter(Boolean).join(" · ");
    const isRunning = log.status === "Running";
    const duration = isRunning
        ? __("Running")
        : format_duration(Number(log.hours) || 0);
    const windowText = session_window(log);

    return `
        <div class="bw-activity-row">
            <span class="bw-project-badge bw-context-colour-${(index % 3) + 1}">
                ${escape_html(context.initials)}
            </span>
            <span class="bw-activity-main">
                <strong>${escape_html(context.title)}</strong>
                <span>${escape_html(contextMeta)}</span>
            </span>
            <span class="bw-activity-log-date">${escape_html(format_log_date(log.log_date))}</span>
            <span class="bw-activity-time">${escape_html(windowText)}</span>
            <span class="bw-activity-duration ${isRunning ? "bw-running-duration" : ""}">
                ${escape_html(duration)}
            </span>
        </div>
    `;
}


function mobile_timer_action({ runningLog, canControl, canSelectContext }) {
    if (!canControl) {
        return "";
    }

    if (runningLog) {
        return `
            <button type="button" class="bw-mobile-action bw-mobile-stop" data-bw-action="stop">
                <span aria-hidden="true">■</span>
                ${__("Stop session")}
            </button>
        `;
    }

    if (!canSelectContext) {
        return "";
    }

    return `
        <button type="button" class="bw-mobile-action bw-mobile-start" data-bw-action="start">
            <span aria-hidden="true">▶</span>
            ${__("Start Session")}
        </button>
    `;
}


/* =========================================================
   INTERACTIONS
========================================================= */

function ensure_timer_widget_dashboard_sync() {
    let bridge = window.__time_trackerTimeTrackerDashboardSyncBridge;

    if (!bridge) {
        const handleUpdate = (payload = {}) => {
            const frm = window.cur_frm;
            const tracker = String(payload.tracker || "");

            if (!is_active_time_tracker_dashboard(frm, tracker)) {
                return;
            }

            schedule_timer_widget_dashboard_sync(frm);
        };

        const domHandler = (event) => {
            handleUpdate((event && event.detail) || {});
        };

        window.addEventListener(BW_TIMER_WIDGET_SYNC_EVENT, domHandler);
        bridge = {
            handleUpdate,
            domHandler,
            realtimeHandler: null,
        };
        window.__time_trackerTimeTrackerDashboardSyncBridge = bridge;
    }

    if (
        !bridge.realtimeHandler
        && frappe.realtime
        && typeof frappe.realtime.on === "function"
    ) {
        bridge.realtimeHandler = (payload = {}) => bridge.handleUpdate(payload);
        frappe.realtime.on("time_tracker_timer_updated", bridge.realtimeHandler);
    }
}


function is_active_time_tracker_dashboard(frm, tracker = "") {
    if (
        !frm
        || !frm.doc
        || (frm.doctype || frm.doc.doctype) !== "Time Tracker"
        || frm.is_new()
        || !frm.doc.name
        || window.cur_frm !== frm
    ) {
        return false;
    }

    if (tracker && tracker !== frm.doc.name) {
        return false;
    }

    const dashboardField = frm.fields_dict && frm.fields_dict.analytics_viewer;
    return Boolean(dashboardField && dashboardField.$wrapper);
}


function schedule_timer_widget_dashboard_sync(frm) {
    if (!is_active_time_tracker_dashboard(frm)) {
        return;
    }

    if (frm.__bw_timer_widget_sync_timer) {
        window.clearTimeout(frm.__bw_timer_widget_sync_timer);
    }

    frm.__bw_timer_widget_sync_timer = window.setTimeout(async () => {
        frm.__bw_timer_widget_sync_timer = null;

        if (!is_active_time_tracker_dashboard(frm)) {
            return;
        }

        if (frm.__bw_local_timer_mutation) {
            return;
        }

        if (frm.__bw_timer_widget_sync_in_flight) {
            frm.__bw_timer_widget_sync_pending = true;
            return;
        }

        frm.__bw_timer_widget_sync_in_flight = true;

        try {
            await reload_dashboard(frm, {silent: true});
        } finally {
            frm.__bw_timer_widget_sync_in_flight = false;

            if (frm.__bw_timer_widget_sync_pending) {
                frm.__bw_timer_widget_sync_pending = false;
                schedule_timer_widget_dashboard_sync(frm);
            }
        }
    }, BW_TIMER_WIDGET_SYNC_DEBOUNCE_MS);
}


function begin_local_timer_mutation(frm) {
    frm.__bw_local_timer_mutation = true;
    frm.__bw_timer_widget_sync_pending = false;

    if (frm.__bw_timer_widget_sync_timer) {
        window.clearTimeout(frm.__bw_timer_widget_sync_timer);
        frm.__bw_timer_widget_sync_timer = null;
    }
}


function end_local_timer_mutation(frm) {
    frm.__bw_local_timer_mutation = false;
    // The local action already completed an authoritative render/reload.
    frm.__bw_timer_widget_sync_pending = false;
}


function signal_browser_widget_refresh(reason, tracker = "") {
    const widget = window.time_trackerTimeTrackerWidget;

    if (!widget) {
        return;
    }

    if (typeof widget.notifyOtherTabs === "function") {
        widget.notifyOtherTabs(reason || "state-changed", tracker);
    }

    if (typeof widget.scheduleRefresh === "function") {
        widget.scheduleRefresh(0);
    } else if (typeof widget.refresh === "function") {
        window.setTimeout(() => widget.refresh(), 0);
    }
}


async function update_browser_widget_preference(frm, enabled, button) {
    const $button = button && button.jquery ? button : $(button || []);

    if (
        !frm.doc.name
        || !$button.length
        || $button.prop("disabled")
        || $button.attr("aria-busy") === "true"
    ) {
        return;
    }

    $button.prop("disabled", true).attr("aria-busy", "true");
    begin_local_timer_mutation(frm);

    try {
        const response = await frappe.call({
            method: "time_tracker.api.set_browser_widget_enabled",
            type: "POST",
            args: {
                tracker: frm.doc.name,
                enabled: enabled ? 1 : 0,
            },
        });
        const result = response.message || {};
        const isEnabled = Boolean(result.enabled);

        frm.dashboard_data = frm.dashboard_data || {};
        frm.dashboard_data.browser_widget_enabled = isEnabled;
        render_dashboard(frm);

        signal_browser_widget_refresh("preference-changed", frm.doc.name);
    } catch (error) {
        console.error("Unable to update Browser Widget preference", error);
    } finally {
        end_local_timer_mutation(frm);
        $button.prop("disabled", false).removeAttr("aria-busy");
    }
}


function bind_dashboard_actions(frm, wrapper) {
    mount_recent_log_date_controls(frm, wrapper);

    wrapper
        .find("[data-bw-record-tab]")
        .off("click")
        .on("click", function () {
            const tab = String($(this).attr("data-bw-record-tab") || "activity");
            frm.__bw_records_tab = tab;

            wrapper.find("[data-bw-record-tab]")
                .removeClass("bw-record-tab-active")
                .attr("aria-selected", "false");
            $(this).addClass("bw-record-tab-active").attr("aria-selected", "true");

            wrapper.find("[data-bw-record-panel]").prop("hidden", true);
            wrapper.find(`[data-bw-record-panel="${tab}"]`).prop("hidden", false);
        });

    wrapper
        .find("[data-bw-salary-view]")
        .off("click")
        .on("click", function () {
            const name = String($(this).attr("data-bw-salary-view") || "");
            if (name) {
                frappe.set_route("Form", "Salary Slip", name);
            }
        });

    wrapper
        .find("[data-bw-salary-download]")
        .off("click")
        .on("click", function () {
            const name = String($(this).attr("data-bw-salary-download") || "");
            if (!name) {
                return;
            }

            const params = new URLSearchParams({
                doctype: "Salary Slip",
                name,
                format: frm.__bw_salary_slip_print_format || BW_SALARY_SLIP_PRINT_FORMAT,
                no_letterhead: "0",
            });
            window.open(
                `/api/method/frappe.utils.print_format.download_pdf?${params.toString()}`,
                "_blank",
                "noopener"
            );
        });

    wrapper
        .find('[data-bw-action="browser-widget-migration-help"]')
        .off("click")
        .on("click", show_browser_widget_migration_required);

    wrapper
        .find('[data-bw-action="toggle-browser-widget"]')
        .off("click")
        .on("click", function () {
            const button = $(this);
            const isEnabled = button.attr("data-bw-enabled") === "1";
            const nextEnabled = !isEnabled;
            const running = Boolean((frm.dashboard_data || {}).running_log);

            const applyPreference = async () => {
                await update_browser_widget_preference(
                    frm,
                    nextEnabled,
                    button
                );
            };

            if (!nextEnabled && running) {
                frappe.confirm(
                    __(
                        "Disabling the Browser Widget only hides the floating timer. "
                        + "Your current session will keep running until you stop it here. Continue?"
                    ),
                    applyPreference
                );
                return;
            }

            applyPreference();
        });

    wrapper
        .find('[data-bw-action="start"]')
        .off("click")
        .on("click", () => show_start_dialog(frm));

    wrapper
        .find('[data-bw-action="stop"]')
        .off("click")
        .on("click", async () => {
            await perform_timer_action(frm, "Stop");
        });

    wrapper
        .find('[data-bw-action="switch"]')
        .off("click")
        .on("click", () => show_start_dialog(frm, {}, { switching: true }));

    wrapper
        .find('[data-bw-action="choose-context"]')
        .off("click")
        .on("click", () => {
            show_start_dialog(
                frm,
                {},
                { switching: Boolean((frm.dashboard_data || {}).running_log) }
            );
        });

    wrapper
        .find("[data-bw-context-index]")
        .off("click")
        .on("click", function () {
            const index = Number($(this).attr("data-bw-context-index"));
            const context = (frm.__bw_quick_contexts || [])[index];

            if (!context) {
                return;
            }

            show_start_dialog(
                frm,
                context.values,
                { switching: Boolean((frm.dashboard_data || {}).running_log) }
            );
        });

    wrapper
        .find('[data-bw-action="previous-week"]')
        .off("click")
        .on("click", async () => {
            frm.week_offset -= 1;
            await reload_dashboard(frm);
        });

    wrapper
        .find('[data-bw-action="next-week"]')
        .off("click")
        .on("click", async () => {
            if (frm.week_offset >= 0) {
                return;
            }

            frm.week_offset += 1;
            await reload_dashboard(frm);
        });

    wrapper
        .find('[data-bw-action="previous-month"]')
        .off("click")
        .on("click", async () => {
            frm.month_offset -= 1;
            await reload_dashboard(frm);
        });

    wrapper
        .find('[data-bw-action="next-month"]')
        .off("click")
        .on("click", async () => {
            if (frm.month_offset >= 0) {
                return;
            }

            frm.month_offset += 1;
            await reload_dashboard(frm);
        });

    wrapper
        .find('[data-bw-action="previous-year"]')
        .off("click")
        .on("click", async () => {
            frm.month_offset -= 12;
            await reload_dashboard(frm);
        });

    wrapper
        .find('[data-bw-action="next-year"]')
        .off("click")
        .on("click", async () => {
            if (frm.month_offset >= 0) {
                return;
            }

            frm.month_offset = Math.min(0, frm.month_offset + 12);
            await reload_dashboard(frm);
        });

    wrapper
        .find('[data-bw-action="load-more-logs"]')
        .off("click")
        .on("click", async function () {
            await load_more_recent_logs(frm, wrapper, $(this));
        });

    wrapper
        .find('[data-bw-action="apply-recent-date-range"]')
        .off("click")
        .on("click", async () => {
            const card = wrapper.find(".bw-activity-card");
            const fromDate = String(
                card.find('[data-bw-date-input="from"]').val() || ""
            );
            const toDate = String(
                card.find('[data-bw-date-input="to"]').val() || ""
            );

            await apply_recent_log_date_range_filter(
                frm,
                wrapper,
                fromDate,
                toDate
            );
        });

    wrapper
        .find('[data-bw-action="clear-recent-date-range"]')
        .off("click")
        .on("click", async () => {
            await apply_recent_log_date_range_filter(frm, wrapper, "", "");
        });
}


function mount_recent_log_date_controls(frm, wrapper) {
    const card = wrapper.find(".bw-activity-card");

    if (!card.length) {
        return;
    }

    const inputs = card.find("[data-bw-date-input]");

    // The dashboard may be opened from a read-only Time Tracker document, but
    // these controls are report filters rather than document fields. Keep them
    // explicitly writable and keyboard accessible for managers as well.
    inputs
        .prop("readOnly", false)
        .prop("disabled", false)
        .attr("aria-readonly", "false")
        .attr("tabindex", "0")
        .off("keydown.bwRecentDate")
        .on("keydown.bwRecentDate", (event) => {
            if (event.key !== "Enter") {
                return;
            }

            event.preventDefault();
            card.find('[data-bw-action="apply-recent-date-range"]').trigger("click");
        });
}


function destroy_recent_log_date_controls(frm) {
    frm.__bw_recent_date_controls = null;
    frm.__bw_recent_date_controls_initialising = false;
}


async function set_recent_log_date_control_values(frm, fromDate, toDate) {
    const wrapper = dashboard_wrapper(frm);
    wrapper.find('[data-bw-date-input="from"]').val(fromDate || "");
    wrapper.find('[data-bw-date-input="to"]').val(toDate || "");
}


function recent_log_date_range_is_valid(fromDate, toDate) {
    if (!fromDate || !toDate) {
        return true;
    }

    const from = moment(fromDate, "YYYY-MM-DD", true);
    const to = moment(toDate, "YYYY-MM-DD", true);

    return from.isValid() && to.isValid() && !from.isAfter(to, "day");
}


async function apply_recent_log_date_range_filter(
    frm,
    wrapper,
    fromDate,
    toDate
) {
    if (frm.__bw_recent_activity_busy) {
        return;
    }

    if (!recent_log_date_range_is_valid(fromDate, toDate)) {
        frappe.show_alert({
            message: __("From Date cannot be after To Date."),
            indicator: "orange",
        });
        return;
    }

    const previousFromDate = frm.__bw_recent_log_from_date || "";
    const previousToDate = frm.__bw_recent_log_to_date || "";
    const card = wrapper.find(".bw-activity-card");

    frm.__bw_recent_activity_busy = true;
    card.attr("aria-busy", "true").addClass("bw-activity-card-loading");
    card.find("input, button").prop("disabled", true);

    try {
        const response = await frappe.call({
            method: "time_tracker.api.get_recent_logs",
            type: "POST",
            args: {
                tracker: frm.doc.name,
                start: 0,
                page_length: frm.__bw_recent_log_page_length || BW_RECENT_LOG_PAGE_LENGTH,
                from_date: fromDate || null,
                to_date: toDate || null,
            },
            freeze: false,
        });
        const result = response.message || {};

        frm.__bw_recent_log_from_date = String(result.from_date || "");
        frm.__bw_recent_log_to_date = String(result.to_date || "");
        frm.__bw_recent_logs = [...(result.logs || [])];
        frm.__bw_recent_logs_has_more = Boolean(result.has_more);

        card.replaceWith(
            recent_sessions_card(
                frm.__bw_recent_logs,
                frm.__bw_recent_logs_has_more,
                frm.__bw_recent_log_from_date,
                frm.__bw_recent_log_to_date
            )
        );
        bind_dashboard_actions(frm, wrapper);
    } catch (error) {
        console.error("Unable to filter Tracker Logs", error);
        frm.__bw_recent_log_from_date = previousFromDate;
        frm.__bw_recent_log_to_date = previousToDate;
        card.attr("aria-busy", "false").removeClass("bw-activity-card-loading");
        card.find("input, button").prop("disabled", false);
        await set_recent_log_date_control_values(
            frm,
            previousFromDate,
            previousToDate
        );
        card
            .find('[data-bw-action="clear-recent-date-range"]')
            .prop("disabled", !(previousFromDate || previousToDate));

        frappe.show_alert({
            message: __("Recent sessions could not be filtered."),
            indicator: "red",
        });
    } finally {
        frm.__bw_recent_activity_busy = false;
    }
}


async function load_more_recent_logs(frm, wrapper, button) {
    if (
        !frm.__bw_recent_logs_has_more
        || button.prop("disabled")
        || frm.__bw_recent_activity_busy
    ) {
        return;
    }

    const originalLabel = button.text().trim();
    const start = (frm.__bw_recent_logs || []).length;

    frm.__bw_recent_activity_busy = true;
    button
        .prop("disabled", true)
        .addClass("bw-is-loading")
        .text(__("Loading..."));

    try {
        const response = await frappe.call({
            method: "time_tracker.api.get_recent_logs",
            type: "POST",
            args: {
                tracker: frm.doc.name,
                start,
                page_length: frm.__bw_recent_log_page_length || BW_RECENT_LOG_PAGE_LENGTH,
                from_date: frm.__bw_recent_log_from_date || null,
                to_date: frm.__bw_recent_log_to_date || null,
            },
            freeze: false,
        });
        const result = response.message || {};
        const existingNames = new Set(
            (frm.__bw_recent_logs || []).map((log) => log.name).filter(Boolean)
        );
        const newLogs = (result.logs || []).filter((log) => (
            !log.name || !existingNames.has(log.name)
        ));
        const rowStart = (frm.__bw_recent_logs || []).length;

        frm.__bw_recent_logs = [
            ...(frm.__bw_recent_logs || []),
            ...newLogs,
        ];
        frm.__bw_recent_logs_has_more = Boolean(result.has_more);

        if (newLogs.length) {
            const rows = recent_session_rows(newLogs, rowStart);
            const list = wrapper.find(".bw-activity-list");

            list.find(".bw-empty-activity").remove();
            list.append(rows);
        }

        if (!frm.__bw_recent_logs_has_more) {
            button.closest(".bw-activity-footer").remove();
            return;
        }

        button
            .prop("disabled", false)
            .removeClass("bw-is-loading")
            .text(originalLabel);
    } catch (error) {
        console.error("Unable to load more Tracker Logs", error);
        button
            .prop("disabled", false)
            .removeClass("bw-is-loading")
            .text(originalLabel);

        frappe.show_alert({
            message: __("More sessions could not be loaded."),
            indicator: "red",
        });
    } finally {
        frm.__bw_recent_activity_busy = false;
    }
}


function show_start_dialog(frm, preset = {}, options = {}) {
    close_context_modal(frm);

    const data = frm.dashboard_data || {};
    const switching = Boolean(options.switching);
    const permissions = data.context_permissions || {};
    const fieldDefinitions = [
        {
            fieldname: "project",
            label: __("Project"),
            options: "Project",
            allowed: Boolean(permissions.project),
        },
        {
            fieldname: "task",
            label: __("Task"),
            options: "Task",
            allowed: Boolean(permissions.task && permissions.project),
        },
        {
            fieldname: "ticket",
            label: __("Ticket"),
            options: data.ticket_doctype || "",
            allowed: Boolean(permissions.ticket && data.ticket_doctype),
        },
    ].filter((definition) => definition.allowed);
    const primaryLabel = switching ? __("Switch session") : __("Start tracking");
    const title = switching ? __("Switch work context") : __("What are you working on?");
    const hasProject = fieldDefinitions.some((field) => field.fieldname === "project");
    const hasTask = fieldDefinitions.some((field) => field.fieldname === "task");
    const hasTicket = fieldDefinitions.some((field) => field.fieldname === "ticket");
    let description = __("Choose an available work context.");

    if (switching) {
        description = __("Choose a new context. The current session will stop before the new one starts.");
    } else if (hasProject && hasTicket) {
        description = hasTask
            ? __("Choose a Project or Ticket. Tasks become available after a Project is selected.")
            : __("Choose a Project or Ticket.");
    } else if (hasProject) {
        description = hasTask
            ? __("Choose a Project. Tasks become available after a Project is selected.")
            : __("Choose a Project.");
    } else if (hasTicket) {
        description = __("Choose a Helpdesk Ticket.");
    }
    const fieldsMarkup = fieldDefinitions.length
        ? fieldDefinitions.map((definition) => `
            <div
                class="bw-context-field"
                data-bw-context-field="${escape_attr(definition.fieldname)}"
            ></div>
        `).join("")
        : `
            <div class="bw-context-no-fields">
                <span aria-hidden="true">◉</span>
                <div>
                    <strong>${__("No work context is available")}</strong>
                    <p>
                        ${escape_html(
                            data.ticket_doctype
                                ? __("Ask an administrator for read access to Project, Task, or Ticket before starting a session.")
                                : __("Ask an administrator for read access to Project or Task before starting a session.")
                        )}
                    </p>
                </div>
            </div>
        `;
    const root = $(`
        <div class="bw-context-modal-backdrop" role="presentation">
            <section
                class="bw-context-modal"
                role="dialog"
                aria-modal="true"
                aria-labelledby="bw-context-modal-title"
            >
                <header class="bw-context-modal-header">
                    <div class="bw-context-modal-title-row">
                        <span class="bw-context-modal-icon" aria-hidden="true">✦</span>
                        <div>
                            <h2 id="bw-context-modal-title">${escape_html(title)}</h2>
                            <p>${escape_html(description)}</p>
                        </div>
                    </div>
                    <button
                        type="button"
                        class="bw-context-modal-close"
                        data-bw-modal-action="close"
                        aria-label="${escape_attr(__("Close"))}"
                    >×</button>
                </header>

                <div class="bw-context-modal-body">
                    <div class="bw-context-fields">${fieldsMarkup}</div>
                    <div class="bw-context-modal-error" role="alert" hidden></div>
                </div>

                <footer class="bw-context-modal-footer">
                    <button
                        type="button"
                        class="bw-context-modal-primary"
                        data-bw-modal-action="submit"
                        ${fieldDefinitions.length ? "" : "disabled"}
                    >
                        <span class="bw-context-primary-label">
                            ${escape_html(
                                fieldDefinitions.length
                                    ? primaryLabel
                                    : __("No available context")
                            )}
                        </span>
                        <span class="bw-context-primary-spinner" aria-hidden="true"></span>
                    </button>
                </footer>
            </section>
        </div>
    `);
    const controls = {};
    const modalState = {
        root,
        busy: false,
        returnFocus: document.activeElement,
    };

    $("body")
        .addClass("bw-context-modal-open")
        .append(root);
    frm.__bw_context_modal = modalState;

    for (const definition of fieldDefinitions) {
        const parent = root.find(`[data-bw-context-field="${definition.fieldname}"]`)[0];
        const control = frappe.ui.form.make_control({
            parent,
            df: {
                fieldtype: "Link",
                fieldname: definition.fieldname,
                label: definition.label,
                options: definition.options,
                placeholder: __("Search {0}", [definition.label]),
            },
            render_input: true,
        });

        control.refresh();
        control.$wrapper.addClass("bw-context-link-control");

        controls[definition.fieldname] = control;

        if (preset[definition.fieldname]) {
            if (typeof control.set_input === "function") {
                control.set_input(preset[definition.fieldname]);
            } else {
                set_context_control_value(control, preset[definition.fieldname]);
            }
        }
    }

    const setTaskAvailability = () => {
        if (!controls.task) {
            return;
        }

        const selectedProject = controls.project?.get_value() || null;
        const taskContainer = root.find('[data-bw-context-field="task"]');
        taskContainer.toggle(Boolean(selectedProject));
        controls.task.$input?.prop("disabled", !selectedProject);

        if (!selectedProject && controls.task.get_value()) {
            set_context_control_value(controls.task, null);
        }
    };

    if (controls.task) {
        controls.task.get_query = () => {
            const selectedProject = controls.project?.get_value() || null;

            return {
                filters: selectedProject
                    ? { project: selectedProject }
                    : { name: ["=", "__no_project_selected__"] },
            };
        };
    }

    if (controls.project && controls.task) {
        const handleProjectChange = () => {
            set_context_control_value(controls.task, null);
            setTaskAvailability();
            show_context_modal_error(root, "");
        };

        // Frappe Link controls may update through typing, selection, or a
        // programmatic preset. Bind both the control callback and concrete
        // input events so Task visibility/query state always follows Project.
        controls.project.df.onchange = handleProjectChange;

        if (controls.project.$input) {
            controls.project.$input
                .off("change.bwTaskAvailability awesomplete-selectcomplete.bwTaskAvailability")
                .on(
                    "change.bwTaskAvailability awesomplete-selectcomplete.bwTaskAvailability",
                    handleProjectChange
                );
        }
    }

    setTaskAvailability();

    const setBusy = (busy) => {
        modalState.busy = busy;
        root.toggleClass("bw-context-modal-busy", busy);
        root
            .find('[data-bw-modal-action="close"]')
            .prop("disabled", busy);
        root
            .find('[data-bw-modal-action="submit"]')
            .prop("disabled", busy || !fieldDefinitions.length);
    };

    const submit = async () => {
        if (modalState.busy) {
            return;
        }

        const values = {};

        for (const definition of fieldDefinitions) {
            const value = controls[definition.fieldname].get_value();
            values[definition.fieldname] = value || null;
        }

        if (values.task && !values.project) {
            show_context_modal_error(root, __("Select a Project before selecting a Task."));
            return;
        }

        if (!Object.values(values).some(Boolean)) {
            show_context_modal_error(
                root,
                __("Select at least one Project, Task, or Ticket before starting a session.")
            );
            return;
        }

        setBusy(true);

        try {
            if (switching) {
                await switch_timer_context(frm, values);
            } else {
                await perform_timer_action(frm, "Start", values);
            }

            close_context_modal(frm);
        } catch (error) {
            console.error("Unable to update timer", error);
            setBusy(false);
        }
    };

    root
        .find('[data-bw-modal-action="close"]')
        .on("click", () => {
            if (!modalState.busy) {
                close_context_modal(frm);
            }
        });

    root
        .find('[data-bw-modal-action="submit"]')
        .on("click", submit);

    root.on("input change", ".bw-context-field input", () => {
        show_context_modal_error(root, "");
    });

    root.on("mousedown", (event) => {
        if (event.target === root[0] && !modalState.busy) {
            close_context_modal(frm);
        }
    });

    $(document)
        .off("keydown.bwContextModal")
        .on("keydown.bwContextModal", (event) => {
            if (event.key === "Escape" && !modalState.busy) {
                close_context_modal(frm);
            }
        });

    window.setTimeout(() => {
        const firstInput = root.find(".bw-context-field input:visible").first();

        if (firstInput.length) {
            firstInput.trigger("focus");
        } else {
            root.find('[data-bw-modal-action="close"]').trigger("focus");
        }
    }, 0);
}


function show_context_modal_error(root, message) {
    const error = root.find(".bw-context-modal-error");
    const text = String(message || "");

    error.text(text).prop("hidden", !text);
}


function set_context_control_value(control, value) {
    if (!control) {
        return;
    }

    const result = control.set_value(value || "");

    if (result && typeof result.catch === "function") {
        result.catch((error) => {
            console.error("Unable to clear context field", error);
        });
    }
}


function close_context_modal(frm) {
    const modal = frm && frm.__bw_context_modal;

    if (!modal) {
        return;
    }

    $(document).off("keydown.bwContextModal");
    modal.root.remove();
    $("body").removeClass("bw-context-modal-open");
    frm.__bw_context_modal = null;

    if (modal.returnFocus && document.contains(modal.returnFocus)) {
        window.setTimeout(() => modal.returnFocus.focus(), 0);
    }
}


async function call_timer_api(frm, action, values = {}, freezeMessage = null) {
    const response = await frappe.call({
        method: "time_tracker.api.toggle_timer",
        type: "POST",
        args: {
            tracker: frm.doc.name,
            action,
            project: values.project || null,
            task: values.task || null,
            ticket: values.ticket || null,
        },
        freeze: false,
    });

    signal_browser_widget_refresh(
        `timer-${String(action || "changed").toLowerCase()}`,
        frm.doc.name
    );

    return response.message || {};
}


async function perform_timer_action(frm, action, values = {}) {
    begin_local_timer_mutation(frm);

    try {
        await call_timer_api(frm, action, values);
        await reload_dashboard(frm);
    } finally {
        end_local_timer_mutation(frm);
    }
}


async function switch_timer_context(frm, values) {
    begin_local_timer_mutation(frm);

    try {
        await call_timer_api(
            frm,
            "Switch",
            values,
            __("Switching work context...")
        );

        await reload_dashboard(frm);
    } finally {
        end_local_timer_mutation(frm);
    }
}


/* =========================================================
   STOPWATCH
========================================================= */

function start_stopwatch(frm, serverElapsedSeconds) {
    clear_stopwatch(frm);

    const data = frm.dashboard_data || {};
    const stats = data.stats || {};
    const daily = stats.daily || {};
    const weekly = stats.weekly || {};
    const monthly = stats.monthly || {};
    const runningLogDate = String((data.running_log || {}).log_date || "");
    const initialElapsed = Math.max(0, Number(serverElapsedSeconds) || 0);
    const initialDailyHours = Number(daily.hours) || 0;
    const initialWeeklyHours = Number(weekly.hours) || 0;
    const initialMonthlyHours = Number(monthly.hours) || 0;
    const advanceDaily = period_contains_date(
        runningLogDate,
        daily.date,
        daily.date
    );
    const advanceWeekly = period_contains_date(
        runningLogDate,
        weekly.start_date,
        weekly.end_date
    );
    const advanceMonthly = period_contains_date(
        runningLogDate,
        monthly.start_date,
        monthly.end_date
    );
    const startedAt = Date.now();
    let tickCount = 0;

    function update_stopwatch() {
        const wrapper = dashboard_wrapper(frm);
        const display = wrapper.find(".bw-timer-value");

        if (!display.length) {
            clear_stopwatch(frm);
            return;
        }

        const clientElapsed = Math.max(0, (Date.now() - startedAt) / 1000);
        const totalElapsed = initialElapsed + clientElapsed;
        const additionalHours = clientElapsed / 3600;
        const currentDailyHours = initialDailyHours + (advanceDaily ? additionalHours : 0);
        const currentWeeklyHours = initialWeeklyHours + (advanceWeekly ? additionalHours : 0);
        const currentMonthlyHours = initialMonthlyHours + (advanceMonthly ? additionalHours : 0);

        display.text(format_clock_seconds(totalElapsed));
        wrapper.find(".bw-today-value").text(format_duration(currentDailyHours));
        wrapper.find(".bw-week-value").text(format_duration(currentWeeklyHours));
        wrapper.find(".bw-month-value").text(format_duration(currentMonthlyHours));
        wrapper.find(".bw-week-goal-copy").text(
            weekly_goal_copy({
                ...weekly,
                hours: currentWeeklyHours,
            })
        );
        wrapper.find(".bw-week-exceeded-value").text(
            format_duration(weekly_exceeded_hours({
                ...weekly,
                hours: currentWeeklyHours,
            }))
        );
        wrapper.find(".bw-week-exceeded-copy").text(
            weekly_exceeded_copy({
                ...weekly,
                hours: currentWeeklyHours,
            })
        );
        wrapper.find(".bw-month-goal-copy").text(
            monthly_goal_copy({
                ...monthly,
                hours: currentMonthlyHours,
            })
        );

        const weeklyLimit = Number(weekly.limit) || 0;

        if (weeklyLimit > 0) {
            const progress = Math.min(100, Math.max(0, (currentWeeklyHours / weeklyLimit) * 100));
            wrapper
                .find(".bw-goal-fill")
                .css("--bw-goal-progress", `${progress}%`)
                .toggleClass("bw-goal-fill-exceeded", currentWeeklyHours > weeklyLimit);
        }

        tickCount += 1;

        if (tickCount % 60 === 0 && moment().format("YYYY-MM-DD") !== data.today) {
            reload_dashboard(frm);
        }
    }

    update_stopwatch();
    frm.custom_timer_interval = setInterval(update_stopwatch, BW_TIMER_INTERVAL_MS);
}


function period_contains_date(dateValue, startDate, endDate) {
    const date = moment(dateValue, "YYYY-MM-DD", true);
    const start = moment(startDate, "YYYY-MM-DD", true);
    const end = moment(endDate, "YYYY-MM-DD", true);

    return Boolean(
        date.isValid()
        && start.isValid()
        && end.isValid()
        && date.isSameOrAfter(start, "day")
        && date.isSameOrBefore(end, "day")
    );
}


function clear_stopwatch(frm) {
    if (frm.custom_timer_interval) {
        clearInterval(frm.custom_timer_interval);
        frm.custom_timer_interval = null;
    }
}


/* =========================================================
   DERIVED DATA
========================================================= */

function recent_contexts(logs, currentLog = null) {
    const seen = new Set();
    const contexts = [];
    const currentKey = currentLog
        ? [currentLog.project || null, currentLog.task || null, currentLog.ticket || null].join("|")
        : null;

    for (const log of logs) {
        const values = {
            project: log.project || null,
            task: log.task || null,
            ticket: log.ticket || null,
        };

        if (!values.project && !values.task && !values.ticket) {
            continue;
        }

        const key = [values.project, values.task, values.ticket].join("|");

        if (key === currentKey || seen.has(key)) {
            continue;
        }

        seen.add(key);

        const details = context_details(log);
        contexts.push({
            ...details,
            values,
        });

        if (contexts.length >= BW_RECENT_CONTEXT_LIMIT) {
            break;
        }
    }

    return contexts;
}


function context_details(log) {
    const project = log.project || null;
    const projectName = log.project_name || project;
    const task = log.task || null;
    const taskName = log.task_name || task;
    const ticket = log.ticket || null;
    const contextRestricted = Boolean(log.context_restricted);

    const projectLabel = id_and_name(project, projectName);
    const taskLabel = id_and_name(task, taskName);
    const title = taskLabel
        || ticket
        || projectLabel
        || (contextRestricted ? __("Restricted work context") : __("General work"));
    const metaParts = [];

    if (task) {
        metaParts.push(__("Task"));
        if (projectLabel) {
            metaParts.push(projectLabel);
        }
    } else if (ticket) {
        metaParts.push(__("Ticket"));
        if (projectLabel) {
            metaParts.push(projectLabel);
        }
    } else if (project) {
        metaParts.push(__("Project"));
    } else if (contextRestricted) {
        metaParts.push(__("Linked context hidden by permissions"));
    } else {
        metaParts.push(__("No linked context"));
    }

    return {
        title,
        meta: metaParts.join(" · "),
        initials: context_initials(task || ticket || project || title),
    };
}


function id_and_name(id, displayName) {
    if (!id) {
        return "";
    }

    const cleanName = String(displayName || "").trim();

    if (!cleanName || cleanName === id) {
        return id;
    }

    return `${id} — ${cleanName}`;
}


function person_initials(fullName) {
    const parts = String(fullName || "")
        .trim()
        .split(/\s+/)
        .filter(Boolean);

    if (!parts.length) {
        return "";
    }

    if (parts.length === 1) {
        return parts[0].slice(0, 2).toUpperCase();
    }

    return `${parts[0][0]}${parts[parts.length - 1][0]}`.toUpperCase();
}


function context_initials(value) {
    const words = String(value || "")
        .replace(/[^a-zA-Z0-9\s-]/g, " ")
        .split(/[\s-]+/)
        .filter(Boolean);

    if (!words.length) {
        return "GW";
    }

    if (words.length === 1) {
        return words[0].slice(0, 2).toUpperCase();
    }

    return `${words[0][0]}${words[1][0]}`.toUpperCase();
}


function hours_for_date(data, dateString) {
    const heatmap = ((data.heatmap || {}).data) || {};
    const daily = (data.stats || {}).daily || {};
    let hours = Number(heatmap[dateString]) || 0;

    if (dateString === daily.date) {
        hours += Number(daily.running_hours) || 0;
    }

    return hours;
}


function week_days(data) {
    const weekly = (data.stats || {}).weekly || {};
    const start = moment(weekly.start_date, "YYYY-MM-DD");
    const days = [];

    for (let index = 0; index < 7; index += 1) {
        const current = start.clone().add(index, "days");
        const dateString = current.format("YYYY-MM-DD");

        days.push({
            date: dateString,
            label: dateString === data.today ? __("Today") : current.format("ddd"),
            hours: hours_for_date(data, dateString),
            isToday: dateString === data.today,
        });
    }

    return days;
}


function current_streak(data) {
    let cursor = moment(data.today, "YYYY-MM-DD");
    let streak = 0;

    if (hours_for_date(data, cursor.format("YYYY-MM-DD")) <= 0) {
        cursor.subtract(1, "day");
    }

    while (hours_for_date(data, cursor.format("YYYY-MM-DD")) > 0) {
        streak += 1;
        cursor.subtract(1, "day");
    }

    return streak;
}


function best_week_hours(data, startDate, endDate) {
    const monthStart = moment(startDate, "YYYY-MM-DD", true);
    const monthEnd = moment(endDate, "YYYY-MM-DD", true);

    if (!monthStart.isValid() || !monthEnd.isValid()) {
        return 0;
    }

    const weekCursor = monthStart.clone().startOf("isoWeek");
    let best = 0;

    while (weekCursor.isSameOrBefore(monthEnd, "day")) {
        let total = 0;

        for (let day = 0; day < 7; day += 1) {
            const current = weekCursor.clone().add(day, "days");

            if (current.isBefore(monthStart, "day") || current.isAfter(monthEnd, "day")) {
                continue;
            }

            total += hours_for_date(data, current.format("YYYY-MM-DD"));
        }

        best = Math.max(best, total);
        weekCursor.add(1, "week");
    }

    return best;
}



/* =========================================================
   COPY AND FORMATTING
========================================================= */

function first_and_last_name(fullName) {
    const nameParts = String(fullName || "")
        .trim()
        .split(/\s+/)
        .filter(Boolean);

    if (nameParts.length <= 1) {
        return nameParts[0] || "";
    }

    return `${nameParts[0]} ${nameParts[nameParts.length - 1]}`;
}


function greeting_text(displayName) {
    const hour = moment().hour();
    let greeting = __("Welcome back");

    if (hour < 12) {
        greeting = __("Good morning");
    } else if (hour < 18) {
        greeting = __("Good afternoon");
    } else {
        greeting = __("Good evening");
    }

    return displayName ? `${greeting}, ${displayName}.` : `${greeting}.`;
}


function greeting_support_text({ weekly, isRunning, canControl }) {
    if (!canControl) {
        return __("Review this employee's timer, goals, and recent sessions in one place.");
    }

    if (isRunning) {
        return __("Your session is live. Stay focused and stop it when the work context changes.");
    }

    const remaining = Math.max(0, (Number(weekly.limit) || 0) - (Number(weekly.hours) || 0));

    if (remaining > 0) {
        return __("You have {0} remaining to reach this week's goal.", [format_duration(remaining)]);
    }

    return __("Your weekly goal is complete. Keep the momentum going.");
}


function weekly_goal_copy(weekly) {
    const hours = Number(weekly.hours) || 0;
    const limit = Number(weekly.limit) || 0;

    if (!limit) {
        return __("No weekly goal configured");
    }

    const difference = limit - hours;

    if (difference > 0) {
        return __("{0} to your weekly goal", [format_duration(difference)]);
    }

    if (difference < 0) {
        return __("{0} over your weekly goal", [format_duration(Math.abs(difference))]);
    }

    return __("Weekly goal reached");
}


function weekly_exceeded_hours(weekly) {
    const hours = Number(weekly.hours) || 0;
    const limit = Number(weekly.limit) || 0;

    return limit > 0 ? Math.max(0, hours - limit) : 0;
}


function weekly_exceeded_copy(weekly) {
    const limit = Number(weekly.limit) || 0;
    const exceeded = weekly_exceeded_hours(weekly);

    if (!limit) {
        return __("No weekly limit configured");
    }

    if (exceeded > 0) {
        return __("Over the weekly limit of {0}", [format_duration(limit)]);
    }

    return __("Within the weekly limit of {0}", [format_duration(limit)]);
}


function monthly_goal_copy(monthly) {
    const hours = Number(monthly.hours) || 0;
    const limit = Number(monthly.limit) || 0;

    if (!limit) {
        return __("No monthly goal configured");
    }

    const difference = limit - hours;

    if (difference > 0) {
        return __("{0} to your monthly goal", [format_duration(difference)]);
    }

    if (difference < 0) {
        return __("{0} over your monthly goal", [format_duration(Math.abs(difference))]);
    }

    return __("Monthly goal reached");
}


function session_window(log) {
    if (!log.start_time) {
        return "—";
    }

    const start = format_time(log.start_time);
    const end = log.end_time ? format_time(log.end_time) : __("Now");

    return `${start} – ${end}`;
}


function format_time(value) {
    if (!value) {
        return "—";
    }

    const userValue = String(frappe.datetime.str_to_user(value));
    const timeMatch = userValue.match(
        /(?:^|\s)(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?)(?:\s|$)/i
    );

    return timeMatch ? timeMatch[1] : userValue;
}


function format_clock_seconds(secondsValue) {
    const totalSeconds = Math.max(0, Math.floor(Number(secondsValue) || 0));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;

    return [hours, minutes, seconds]
        .map((value) => String(value).padStart(2, "0"))
        .join(":");
}


function format_duration(decimalHours) {
    const totalMinutes = Math.round(Math.max(0, Number(decimalHours) || 0) * 60);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;

    if (!hours && !minutes) {
        return "0m";
    }

    if (!hours) {
        return `${minutes}m`;
    }

    if (!minutes) {
        return `${hours}h`;
    }

    return `${hours}h ${minutes}m`;
}


function format_employee_date(value) {
    if (!value) {
        return __("Not set");
    }

    const parsed = moment(value, "YYYY-MM-DD", true);
    return parsed.isValid() ? parsed.format("D MMM YYYY") : String(value);
}


function format_long_date(value) {
    const parsed = moment(value, "YYYY-MM-DD");
    return parsed.isValid() ? parsed.format("dddd, D MMMM") : (value || "");
}


function format_short_date(value) {
    const parsed = moment(value, "YYYY-MM-DD");
    return parsed.isValid() ? parsed.format("ddd, D MMM") : (value || "");
}


function format_log_date(value) {
    const parsed = moment(value, "YYYY-MM-DD", true);
    return parsed.isValid() ? parsed.format("D MMM YYYY") : (value || "—");
}


function format_week_range(startDate, endDate) {
    const start = moment(startDate, "YYYY-MM-DD");
    const end = moment(endDate, "YYYY-MM-DD");

    if (!start.isValid() || !end.isValid()) {
        return "";
    }

    if (start.year() === end.year() && start.month() === end.month()) {
        return `${start.format("D")}–${end.format("D MMM YYYY")}`;
    }

    if (start.year() === end.year()) {
        return `${start.format("D MMM")} – ${end.format("D MMM YYYY")}`;
    }

    return `${start.format("D MMM YYYY")} – ${end.format("D MMM YYYY")}`;
}


function format_month_label(value) {
    const parsed = moment(value, "YYYY-MM-DD", true);
    return parsed.isValid() ? parsed.format("MMMM YYYY") : (value || "");
}


function escape_html(value) {
    return frappe.utils.escape_html(String(value ?? ""));
}


function escape_attr(value) {
    return escape_html(value);
}