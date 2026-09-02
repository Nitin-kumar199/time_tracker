frappe.listview_settings["Time Tracker"] = {
    add_fields: ["employee", "employee_name", "status"],


    get_indicator(doc) {
        const status = doc.status || __("Not Set");

        const colours = {
            Active: "green",
            Inactive: "gray",
            Suspended: "orange",
            Left: "red",
        };

        return [
            __(status),
            colours[status] || "gray",
            ["status", "=", status],
        ];
    },
};
