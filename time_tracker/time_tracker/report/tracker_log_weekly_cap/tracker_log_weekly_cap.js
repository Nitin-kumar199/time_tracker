frappe.query_reports["Tracker Log Weekly Cap"] = {
    filters: [
        {
            fieldname: "employee",
            label: __("Employee"),
            fieldtype: "Link",
            options: "Employee",
            reqd: 1,
        },
        {
            fieldname: "month",
            label: __("Month"),
            fieldtype: "Select",
            options: [
                "January",
                "February",
                "March",
                "April",
                "May",
                "June",
                "July",
                "August",
                "September",
                "October",
                "November",
                "December",
            ],
            default: moment().format("MMMM"),
            reqd: 1,
        },
        {
            fieldname: "year",
            label: __("Year"),
            fieldtype: "Int",
            default: moment().year(),
            reqd: 1,
        },
    ],
};
