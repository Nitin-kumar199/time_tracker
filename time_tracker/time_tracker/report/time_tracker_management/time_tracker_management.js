frappe.query_reports["Time Tracker Management"] = {
    filters: [
        {
            fieldname: "from_date",
            label: __("From Date"),
            fieldtype: "Date",
            default: frappe.datetime.month_start(),
            reqd: 1,
        },
        {
            fieldname: "to_date",
            label: __("To Date"),
            fieldtype: "Date",
            default: frappe.datetime.get_today(),
            reqd: 1,
        },
        {
            fieldname: "company",
            label: __("Company"),
            fieldtype: "Link",
            options: "Company",
        },
        {
            fieldname: "department",
            label: __("Department"),
            fieldtype: "Link",
            options: "Department",
        },
        {
            fieldname: "employee_status",
            label: __("Employee Status"),
            fieldtype: "Select",
            options: "\nActive\nInactive\nSuspended\nLeft",
        },
        {
            fieldname: "timer_status",
            label: __("Timer Status"),
            fieldtype: "Select",
            options: "\nRunning\nIdle\nNo Tracker",
        },
        {
            fieldname: "employee",
            label: __("Employee"),
            fieldtype: "Link",
            options: "Employee",
        },
    ],

    formatter(value, row, column, data, default_formatter) {
        const formatted = default_formatter(value, row, column, data);

        if (column.fieldname === "timer_status") {
            const indicator = value === "Running" ? "green" : (value === "No Tracker" ? "yellow" : "gray");
            return `<span class="indicator-pill ${indicator}">${frappe.utils.escape_html(value || "")}</span>`;
        }

        if (["employee_status", "tracker_status"].includes(column.fieldname)) {
            const indicators = {
                Active: "green",
                Inactive: "gray",
                Suspended: "orange",
                Left: "red",
                "No Tracker": "yellow",
            };
            const indicator = indicators[value] || "gray";
            return `<span class="indicator-pill ${indicator}">${frappe.utils.escape_html(value || "")}</span>`;
        }

        return formatted;
    },
};
