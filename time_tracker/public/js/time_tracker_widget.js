(() => {
    "use strict";

    const STATE_METHOD = "time_tracker.api.get_timer_widget_state";
    const TOGGLE_METHOD = "time_tracker.api.toggle_timer";
    const REFRESH_INTERVAL_MS = 60 * 1000;
    const TAB_SYNC_DEBOUNCE_MS = 80;
    const TAB_SYNC_CHANNEL_PREFIX = "time_tracker-time-tracker";
    const TAB_SYNC_STORAGE_PREFIX = "time_tracker:time-tracker-sync";
    const TIMER_STATE_EVENT = "time_tracker:timer-state-changed";
    const WIDGET_BUILD = "0.6.0";

    window.time_trackerTimeTrackerWidgetBuild = WIDGET_BUILD;

    function escapeHtml(value) {
        const node = document.createElement("div");
        node.textContent = String(value ?? "");
        return node.innerHTML;
    }

    function escapeAttr(value) {
        return escapeHtml(value).replace(/`/g, "&#96;");
    }

    class TimeTrackerTimerWidget {
        constructor() {
            this.state = null;
            this.busy = false;
            this.baseElapsedSeconds = 0;
            this.syncedAt = Date.now();
            this.refreshTimer = null;
            this.tickTimer = null;
            this.refreshInFlight = false;
            this.refreshQueued = false;
            this.stateRequestSerial = 0;
            this.instanceId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
            this.syncChannel = null;
            this.syncStorageKey = "";
            this.syncRefreshTimer = null;
            this.startDialog = null;
            this.$root = null;

            this.createElement();
            this.bindGlobalEvents();
            this.startTimers();
            this.refresh();
        }

        createElement() {
            const root = document.createElement("div");
            root.id = "time_tracker-time-tracker-widget";
            root.className = "time_tracker-timer-widget";
            root.hidden = true;
            root.setAttribute("role", "group");
            root.setAttribute("aria-label", __("Time Tracker"));

            root.innerHTML = `
                <button type="button" class="time_tracker-timer-action">
                    <span class="time_tracker-timer-action-icon" aria-hidden="true"></span>
                </button>
                <button type="button" class="time_tracker-timer-display">
                    <span class="time_tracker-timer-value">0:00</span>
                </button>
                <button type="button" class="time_tracker-timer-open">
                    <span class="time_tracker-timer-open-icon" aria-hidden="true">
                        <span></span>
                        <span></span>
                        <span></span>
                        <span></span>
                    </span>
                </button>
            `;

            document.body.appendChild(root);
            this.$root = root;
            this.$value = root.querySelector(".time_tracker-timer-value");
            this.$display = root.querySelector(".time_tracker-timer-display");
            this.$action = root.querySelector(".time_tracker-timer-action");
            this.$open = root.querySelector(".time_tracker-timer-open");

            this.$display.setAttribute("aria-label", __("Open Time Tracker dashboard"));
            this.$display.title = __("Open Time Tracker dashboard");
            this.$action.setAttribute("aria-label", __("Start timer"));
            this.$action.title = __("Start timer");
            this.$open.setAttribute("aria-label", __("Open Time Tracker dashboard"));
            this.$open.title = __("Open Time Tracker dashboard");

            this.$action.addEventListener("click", () => this.handleAction());
            this.$display.addEventListener("click", () => this.openTracker());
            this.$open.addEventListener("click", () => this.openTracker());
        }

        bindGlobalEvents() {
            window.addEventListener("focus", () => this.scheduleRefresh(0));
            document.addEventListener("visibilitychange", () => {
                if (!document.hidden) {
                    this.scheduleRefresh(0);
                }
            });

            this.bindCrossTabEvents();

            if (frappe.realtime && typeof frappe.realtime.on === "function") {
                frappe.realtime.on("time_tracker_timer_updated", (payload = {}) => {
                    // The event is scoped to the affected User on the server.
                    // Refresh both the compact widget and any open Time Tracker
                    // dashboard in this browser tab.
                    this.handleExternalUpdate(payload, "realtime");
                });
            }
        }

        getCrossTabScope() {
            const site = (
                frappe.boot
                && (frappe.boot.sitename || frappe.boot.site_name)
            ) || window.location.host;
            const user = (frappe.session && frappe.session.user) || "Guest";
            return encodeURIComponent(`${site}|${user}`);
        }

        bindCrossTabEvents() {
            const scope = this.getCrossTabScope();
            this.syncStorageKey = `${TAB_SYNC_STORAGE_PREFIX}:${scope}`;

            window.addEventListener("storage", (event) => {
                if (event.key !== this.syncStorageKey || !event.newValue) {
                    return;
                }

                let payload = {};
                try {
                    payload = JSON.parse(event.newValue) || {};
                    if (payload.source === this.instanceId) {
                        return;
                    }
                } catch (error) {
                    // A malformed value should not prevent the normal refresh.
                }

                this.handleExternalUpdate(payload, "storage");
            });

            if (typeof window.BroadcastChannel !== "function") {
                return;
            }

            try {
                this.syncChannel = new window.BroadcastChannel(
                    `${TAB_SYNC_CHANNEL_PREFIX}:${scope}`
                );
                this.syncChannel.addEventListener("message", (event) => {
                    const payload = event.data || {};
                    if (payload.source === this.instanceId) {
                        return;
                    }
                    this.handleExternalUpdate(payload, "broadcast");
                });
            } catch (error) {
                // localStorage remains as the cross-tab fallback.
                this.syncChannel = null;
            }
        }

        scheduleRefresh(delay = TAB_SYNC_DEBOUNCE_MS) {
            if (this.syncRefreshTimer) {
                window.clearTimeout(this.syncRefreshTimer);
            }

            this.syncRefreshTimer = window.setTimeout(() => {
                this.syncRefreshTimer = null;
                this.refresh();
            }, Math.max(0, Number(delay) || 0));
        }

        handleExternalUpdate(payload = {}, origin = "external") {
            this.scheduleRefresh();
            this.emitTimerStateChange({
                tracker: payload.tracker || (this.state && this.state.tracker) || "",
                reason: payload.reason || "state-changed",
                origin,
                source: payload.source || "",
                at: payload.at || Date.now(),
            });
        }

        emitTimerStateChange({
            tracker = "",
            reason = "state-changed",
            origin = "widget",
            source = this.instanceId,
            at = Date.now(),
        } = {}) {
            const detail = {
                tracker: String(tracker || ""),
                reason: String(reason || "state-changed"),
                origin: String(origin || "widget"),
                source: String(source || ""),
                at: Number(at) || Date.now(),
            };

            try {
                window.dispatchEvent(new CustomEvent(TIMER_STATE_EVENT, {detail}));
            } catch (error) {
                // CustomEvent is available in supported Desk browsers. Keep a
                // small fallback so an older embedded browser still receives it.
                const event = document.createEvent("CustomEvent");
                event.initCustomEvent(TIMER_STATE_EVENT, false, false, detail);
                window.dispatchEvent(event);
            }
        }

        notifyOtherTabs(reason = "state-changed", tracker = "") {
            const payload = {
                type: "refresh",
                tracker: tracker || (this.state && this.state.tracker) || "",
                reason: String(reason || "state-changed"),
                source: this.instanceId,
                at: Date.now(),
            };

            if (this.syncChannel) {
                try {
                    this.syncChannel.postMessage(payload);
                } catch (error) {
                    // The localStorage fallback below still runs.
                }
            }

            if (!this.syncStorageKey) {
                return;
            }

            try {
                window.localStorage.setItem(
                    this.syncStorageKey,
                    JSON.stringify(payload)
                );
            } catch (error) {
                // Storage can be disabled by browser privacy settings. The
                // server realtime event and periodic refresh still apply.
            }
        }

        startTimers() {
            this.tickTimer = window.setInterval(() => this.renderClock(), 1000);
            this.refreshTimer = window.setInterval(
                () => this.refresh(),
                REFRESH_INTERVAL_MS
            );
        }

        async refresh() {
            if (frappe.session.user === "Guest") {
                return;
            }

            if (this.refreshInFlight) {
                this.refreshQueued = true;
                return;
            }

            if (this.busy) {
                return;
            }

            this.refreshInFlight = true;
            const requestSerial = ++this.stateRequestSerial;
            try {
                const response = await frappe.call({
                    method: STATE_METHOD,
                    type: "POST",
                });
                if (requestSerial === this.stateRequestSerial) {
                    this.applyState(response.message || {});
                }
            } catch (error) {
                // A route should never become unusable because the optional
                // compact widget could not refresh. Preserve a previously
                // visible widget through transient network/realtime failures.
                if (!this.state || !this.state.available) {
                    this.hide();
                }
                console.error("Time Tracker widget refresh failed", error);
            } finally {
                this.refreshInFlight = false;

                if (this.refreshQueued) {
                    this.refreshQueued = false;
                    window.setTimeout(() => this.refresh(), 0);
                }
            }
        }

        applyState(state) {
            this.state = state;

            if (!state.available) {
                this.baseElapsedSeconds = 0;
                this.syncedAt = Date.now();
                this.$root.classList.remove("is-running", "is-disabled", "is-busy");
                this.$value.textContent = "0:00";
                this.closeStartDialog({restoreFocus: false});
                this.hide();
                return;
            }

            this.baseElapsedSeconds = state.running
                ? Number(state.running.elapsed_seconds || 0)
                : 0;
            this.syncedAt = Date.now();
            this.$root.hidden = false;
            this.$root.classList.toggle("is-running", Boolean(state.running));
            this.$root.classList.toggle("is-disabled", !state.can_control);

            const isRunning = Boolean(state.running);
            const actionLabel = isRunning ? __("Stop timer") : __("Start timer");
            this.$action.setAttribute("aria-label", actionLabel);
            this.$action.title = actionLabel;
            this.$action.disabled = this.busy || !state.can_control;

            if (isRunning && this.startDialog && !this.busy) {
                this.closeStartDialog({restoreFocus: false});
            }

            const context = this.getContextLabel(state.running);
            const employeeName = state.employee_name || state.employee || __("Employee");
            const statusText = state.error
                || (
                    isRunning
                        ? __("Running{0}", [context ? `: ${context}` : ""])
                        : state.can_control
                            ? __("Timer stopped")
                            : __("Timer unavailable while Employee is {0}", [
                                state.employee_status || __("Inactive"),
                            ])
                );
            this.$root.title = `${employeeName} — ${statusText}`;
            this.renderClock();
        }

        hide() {
            if (this.$root) {
                this.$root.hidden = true;
            }
        }

        renderClock() {
            if (!this.$value) {
                return;
            }

            let seconds = 0;
            if (this.state && this.state.running) {
                seconds = this.baseElapsedSeconds
                    + Math.max(0, Math.floor((Date.now() - this.syncedAt) / 1000));
            }

            const compactDuration = this.formatCompactDuration(seconds);
            const fullDuration = this.formatFullDuration(seconds);
            this.$value.textContent = compactDuration;
            this.$display.setAttribute(
                "aria-label",
                __("Open Time Tracker dashboard. Elapsed time {0}", [fullDuration])
            );
            this.$display.title = `${__("Open Time Tracker dashboard")} — ${fullDuration}`;
        }

        formatCompactDuration(totalSeconds) {
            const safeSeconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
            const hours = Math.floor(safeSeconds / 3600);
            const minutes = Math.floor((safeSeconds % 3600) / 60);
            const seconds = safeSeconds % 60;

            if (hours) {
                return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
            }

            return `${minutes}:${String(seconds).padStart(2, "0")}`;
        }

        formatFullDuration(totalSeconds) {
            const safeSeconds = Math.max(0, Math.floor(Number(totalSeconds) || 0));
            const hours = Math.floor(safeSeconds / 3600);
            const minutes = Math.floor((safeSeconds % 3600) / 60);
            const seconds = safeSeconds % 60;
            return [hours, minutes, seconds]
                .map((value) => String(value).padStart(2, "0"))
                .join(":");
        }

        getContextLabel(running) {
            if (!running) {
                return "";
            }
            const parts = [];
            if (running.project_name || running.project) {
                parts.push(running.project_name || running.project);
            }
            if (running.task_name || running.task) {
                parts.push(running.task_name || running.task);
            }
            if (running.ticket) {
                parts.push(running.ticket);
            }
            if (!parts.length && running.context_restricted) {
                return __("Restricted work context");
            }
            return parts.join(" / ");
        }

        openTracker() {
            if (!this.state || !this.state.tracker) {
                return;
            }
            this.closeStartDialog({restoreFocus: false});
            frappe.set_route("Form", "Time Tracker", this.state.tracker);
        }

        async handleAction() {
            if (!this.state || this.busy) {
                return;
            }

            if (!this.state.can_control) {
                frappe.show_alert({
                    message: __("This Employee's timer is read-only."),
                    indicator: "orange",
                });
                return;
            }

            if (this.state.running) {
                await this.toggle("Stop");
                return;
            }

            this.openStartDialog();
        }

        openStartDialog() {
            if (this.startDialog) {
                this.focusStartDialog();
                return;
            }

            const jq = window.jQuery || window.$;
            const canMakeControl = Boolean(
                frappe.ui
                && frappe.ui.form
                && typeof frappe.ui.form.make_control === "function"
            );

            if (typeof jq !== "function" || !canMakeControl) {
                frappe.msgprint({
                    title: __("Time Tracker"),
                    message: __("The work-context selector is not ready. Reload the page and try again."),
                    indicator: "orange",
                });
                return;
            }

            const permissions = this.state.context_permissions || {};
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
                    options: this.state.ticket_doctype || "",
                    allowed: Boolean(permissions.ticket && this.state.ticket_doctype),
                },
            ].filter((definition) => definition.allowed);
            const hasProject = fieldDefinitions.some(
                (field) => field.fieldname === "project"
            );
            const hasTask = fieldDefinitions.some(
                (field) => field.fieldname === "task"
            );
            const hasTicket = fieldDefinitions.some(
                (field) => field.fieldname === "ticket"
            );
            let description = __("Choose an available work context.");

            if (hasProject && hasTicket) {
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

            const title = __("What are you working on?");
            const titleId = `bw-widget-context-title-${this.instanceId.replace(/[^a-z0-9_-]/gi, "")}`;
            const fieldsMarkup = fieldDefinitions.length
                ? fieldDefinitions.map((definition) => `
                    <div
                        class="bw-context-field"
                        data-bw-context-field="${escapeAttr(definition.fieldname)}"
                    ></div>
                `).join("")
                : `
                    <div class="bw-context-no-fields">
                        <span aria-hidden="true">◉</span>
                        <div>
                            <strong>${escapeHtml(__("No work context is available"))}</strong>
                            <p>${escapeHtml(
                                this.state.ticket_doctype
                                    ? __("Ask an administrator for read access to Project, Task, or Ticket before starting a session.")
                                    : __("Ask an administrator for read access to Project or Task before starting a session.")
                            )}</p>
                        </div>
                    </div>
                `;
            const root = jq(`
                <div
                    class="bw-context-modal-backdrop bw-widget-context-modal-backdrop"
                    role="presentation"
                >
                    <section
                        class="bw-context-modal"
                        role="dialog"
                        aria-modal="true"
                        aria-labelledby="${escapeAttr(titleId)}"
                    >
                        <header class="bw-context-modal-header">
                            <div class="bw-context-modal-title-row">
                                <span class="bw-context-modal-icon" aria-hidden="true">✦</span>
                                <div>
                                    <h2 id="${escapeAttr(titleId)}">${escapeHtml(title)}</h2>
                                    <p>${escapeHtml(description)}</p>
                                </div>
                            </div>
                            <button
                                type="button"
                                class="bw-context-modal-close"
                                data-bw-modal-action="close"
                                aria-label="${escapeAttr(__("Close"))}"
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
                                    ${escapeHtml(
                                        fieldDefinitions.length
                                            ? __("Start tracking")
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
                controls,
                busy: false,
                returnFocus: document.activeElement,
                keydownHandler: null,
            };

            this.startDialog = modalState;
            document.body.classList.add("bw-widget-context-modal-open");
            jq("body").append(root);

            try {
                for (const definition of fieldDefinitions) {
                    const parent = root.find(
                        `[data-bw-context-field="${definition.fieldname}"]`
                    )[0];
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
                }
            } catch (error) {
                console.error("Unable to create Time Tracker work-context controls", error);
                this.closeStartDialog({restoreFocus: false});
                frappe.msgprint({
                    title: __("Time Tracker"),
                    message: __("The work-context selector could not be created. Reload the page and try again."),
                    indicator: "red",
                });
                return;
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
                    this.setContextControlValue(controls.task, null);
                }
            };

            if (controls.task) {
                controls.task.get_query = () => {
                    const selectedProject = controls.project?.get_value() || null;
                    return {
                        filters: selectedProject
                            ? {project: selectedProject}
                            : {name: ["=", "__no_project_selected__"]},
                    };
                };
            }

            if (controls.project && controls.task) {
                const handleProjectChange = () => {
                    this.setContextControlValue(controls.task, null);
                    setTaskAvailability();
                    this.showStartDialogError("");
                };

                controls.project.df.onchange = handleProjectChange;

                if (controls.project.$input) {
                    controls.project.$input
                        .off(
                            "change.bwWidgetTaskAvailability "
                            + "awesomplete-selectcomplete.bwWidgetTaskAvailability"
                        )
                        .on(
                            "change.bwWidgetTaskAvailability "
                            + "awesomplete-selectcomplete.bwWidgetTaskAvailability",
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

                for (const control of Object.values(controls)) {
                    control.$input?.prop("disabled", busy);
                }

                if (!busy) {
                    setTaskAvailability();
                }
            };

            const submit = async () => {
                if (modalState.busy || this.startDialog !== modalState) {
                    return;
                }

                const values = {};
                for (const definition of fieldDefinitions) {
                    const value = controls[definition.fieldname].get_value();
                    values[definition.fieldname] = value || null;
                }

                if (values.task && !values.project) {
                    this.showStartDialogError(
                        __("Select a Project before selecting a Task.")
                    );
                    return;
                }

                if (!Object.values(values).some(Boolean)) {
                    this.showStartDialogError(
                        __("Select at least one Project, Task, or Ticket before starting a session.")
                    );
                    return;
                }

                setBusy(true);
                this.showStartDialogError("");
                let succeeded = false;

                try {
                    succeeded = await this.toggle("Start", values);
                    if (succeeded) {
                        this.closeStartDialog();
                    } else if (this.startDialog === modalState) {
                        this.showStartDialogError(
                            __("The timer could not be started. Review the message and try again.")
                        );
                    }
                } finally {
                    if (!succeeded && this.startDialog === modalState) {
                        setBusy(false);
                    }
                }
            };

            root
                .find('[data-bw-modal-action="close"]')
                .on("click", () => {
                    if (!modalState.busy) {
                        this.closeStartDialog();
                    }
                });

            root
                .find('[data-bw-modal-action="submit"]')
                .on("click", submit);

            root.on("input change", ".bw-context-field input", () => {
                this.showStartDialogError("");
            });

            root.on("mousedown", (event) => {
                if (event.target === root[0] && !modalState.busy) {
                    this.closeStartDialog();
                }
            });

            modalState.keydownHandler = (event) => {
                if (
                    event.key === "Escape"
                    && !modalState.busy
                    && this.startDialog === modalState
                ) {
                    event.preventDefault();
                    this.closeStartDialog();
                }
            };
            document.addEventListener("keydown", modalState.keydownHandler);

            window.setTimeout(() => this.focusStartDialog(), 0);
        }

        focusStartDialog() {
            if (!this.startDialog) {
                return;
            }

            const root = this.startDialog.root;
            const firstInput = root.find(".bw-context-field:visible input:visible").first();
            if (firstInput.length) {
                firstInput.trigger("focus");
                return;
            }

            root.find('[data-bw-modal-action="close"]').trigger("focus");
        }

        showStartDialogError(message) {
            if (!this.startDialog) {
                return;
            }

            const text = String(message || "");
            this.startDialog.root
                .find(".bw-context-modal-error")
                .text(text)
                .prop("hidden", !text);
        }

        setContextControlValue(control, value) {
            if (!control) {
                return;
            }

            const result = control.set_value(value || "");
            if (result && typeof result.catch === "function") {
                result.catch((error) => {
                    console.error("Unable to update work-context field", error);
                });
            }
        }

        closeStartDialog({restoreFocus = true} = {}) {
            const modal = this.startDialog;
            if (!modal) {
                return;
            }

            this.startDialog = null;

            if (modal.keydownHandler) {
                document.removeEventListener("keydown", modal.keydownHandler);
            }

            modal.root.remove();
            document.body.classList.remove("bw-widget-context-modal-open");

            if (
                restoreFocus
                && modal.returnFocus
                && document.contains(modal.returnFocus)
                && !this.$root.hidden
            ) {
                window.setTimeout(() => modal.returnFocus.focus(), 0);
            }
        }

        async toggle(action, context = {}) {
            if (!this.state || !this.state.tracker || this.busy) {
                return false;
            }

            this.busy = true;
            this.$action.disabled = true;
            this.$root.classList.add("is-busy");

            try {
                await frappe.call({
                    method: TOGGLE_METHOD,
                    type: "POST",
                    args: {
                        tracker: this.state.tracker,
                        action,
                        project: context.project || "",
                        task: context.task || "",
                        ticket: context.ticket || "",
                    },
                });
                const reason = `timer-${String(action || "changed").toLowerCase()}`;
                const tracker = this.state.tracker;
                this.emitTimerStateChange({tracker, reason, origin: "widget"});
                this.notifyOtherTabs(reason);
                try {
                    await this.refreshAfterAction();
                } catch (refreshError) {
                    // The action itself succeeded. Keep the dialog/button flow
                    // correct and retry state synchronisation independently.
                    console.error(
                        "Time Tracker state refresh failed after action",
                        refreshError
                    );
                    window.setTimeout(() => this.refresh(), 1000);
                }
                return true;
            } catch (error) {
                console.error("Time Tracker action failed", error);
                return false;
            } finally {
                this.busy = false;
                this.$root.classList.remove("is-busy");
                if (this.state) {
                    this.$action.disabled = !this.state.can_control;
                }
            }
        }

        async refreshAfterAction() {
            // refresh() normally skips while busy; issue a newer request directly.
            // Any slower pre-action refresh is ignored by the serial check.
            const requestSerial = ++this.stateRequestSerial;
            const response = await frappe.call({
                method: STATE_METHOD,
                type: "POST",
            });
            if (requestSerial === this.stateRequestSerial) {
                this.applyState(response.message || {});
            }
        }
    }

    let initialiseAttempts = 0;
    const MAX_INITIALISE_ATTEMPTS = 300;

    function initialise() {
        if (window.time_trackerTimeTrackerWidget) {
            return;
        }

        if (
            typeof frappe === "undefined"
            || !frappe.session
            || typeof __ !== "function"
            || !document.body
        ) {
            initialiseAttempts += 1;
            if (initialiseAttempts <= MAX_INITIALISE_ATTEMPTS) {
                window.setTimeout(initialise, 100);
            }
            return;
        }

        if (frappe.session.user === "Guest") {
            return;
        }

        window.time_trackerTimeTrackerWidget = new TimeTrackerTimerWidget();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initialise, {once: true});
    } else {
        initialise();
    }

    // A slow Desk boot should not permanently skip the widget after the first
    // DOM event. The global guard keeps this fallback idempotent.
    window.addEventListener("load", initialise, {once: true});
})();
