from __future__ import annotations

import frappe


ROLE_MANAGER = "Time Tracker Manager"
ROLE_LOG_EDITOR = "Time Tracker Log Editor"
ROLE_HR_MANAGER = "HR Manager"

READ_PERMISSION_TYPES = {
    None,
    "read",
    "select",
    "report",
    "print",
    "email",
    "export",
}


def is_system_manager(user: str | None = None) -> bool:
    user = user or frappe.session.user
    return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def is_hr_manager(user: str | None = None) -> bool:
    user = user or frappe.session.user
    return ROLE_HR_MANAGER in frappe.get_roles(user)


def is_tracker_log_editor(user: str | None = None) -> bool:
    user = user or frappe.session.user
    return is_system_manager(user) or ROLE_LOG_EDITOR in frappe.get_roles(user)


def get_employees_for_user(
    user: str | None = None,
    *,
    active_only: bool = False,
) -> list[str]:
    """Return every Employee linked to a User, preferring active records.

    A manager can have more than one Employee record after a transfer or rehire.
    A reportee may still point at an older linked Employee, so read scope must
    consider every Employee linked to the same User rather than one arbitrary
    record.
    """

    user = user or frappe.session.user

    if user in {"Guest", "Administrator"}:
        return []

    rows = frappe.get_all(
        "Employee",
        filters={"user_id": user},
        fields=["name", "status", "modified"],
        order_by="modified desc",
        limit_page_length=0,
    )

    active = [row.name for row in rows if row.status == "Active"]
    if active_only:
        return active

    historical = [row.name for row in rows if row.status != "Active"]
    return active + historical


def get_employee_for_user(
    user: str | None = None,
    *,
    active_only: bool = True,
) -> str | None:
    """Return the preferred Employee linked to a User."""

    employees = get_employees_for_user(user, active_only=active_only)
    return employees[0] if employees else None


def get_reportees(manager_employee: str) -> set[str]:
    """Return every direct and indirect reportee, regardless of status."""

    if not manager_employee:
        return set()

    roots = {manager_employee}
    found: set[str] = set()
    frontier = set(roots)

    while frontier:
        children = set(
            frappe.get_all(
                "Employee",
                filters={"reports_to": ["in", sorted(frontier)]},
                pluck="name",
                limit_page_length=0,
            )
        )
        new_children = children - roots - found

        if not new_children:
            break

        found.update(new_children)
        frontier = new_children

    return found


def visible_employees(user: str | None = None) -> set[str] | None:
    """Return Employees whose Time Tracker data the user may read.

    ``None`` means unrestricted access. An empty set means no Employee scope.
    HR Manager and the dedicated log editor can review all Employees. Every
    linked Employee can read their own tracker. Direct and indirect reportees
    defined through ``Employee.reports_to`` are also readable; write access
    remains restricted to the tracker owner.
    """

    user = user or frappe.session.user
    roles = set(frappe.get_roles(user))

    if (
        is_system_manager(user)
        or ROLE_LOG_EDITOR in roles
        or ROLE_HR_MANAGER in roles
    ):
        return None

    linked_employees = get_employees_for_user(user, active_only=False)
    if not linked_employees:
        return set()

    visible = set(linked_employees)

    # The Employee.reports_to hierarchy is the source of truth for manager
    # visibility. The dedicated role is still used for report access and role
    # permissions, but a linked Employee who genuinely has reportees can read
    # those existing trackers even if the role assignment was delayed. Write
    # access remains owner-only in ``time_tracker_has_permission``.
    for manager_employee in linked_employees:
        visible.update(get_reportees(manager_employee))

    return visible


def can_read_employee(employee: str, user: str | None = None) -> bool:
    visible = visible_employees(user)
    return visible is None or employee in visible


def _query_condition(doctype: str, user: str | None = None) -> str:
    visible = visible_employees(user)

    if visible is None:
        return ""

    if not visible:
        return "1=0"

    quoted = ", ".join(frappe.db.escape(employee) for employee in sorted(visible))
    return f"`tab{doctype}`.`employee` IN ({quoted})"


def time_tracker_query(user: str | None = None) -> str:
    return _query_condition("Time Tracker", user)


def tracker_log_query(user: str | None = None) -> str:
    return _query_condition("Tracker Log", user)


def time_tracker_has_permission(
    doc,
    user: str | None = None,
    ptype: str | None = None,
    debug: bool = False,
    permission_type: str | None = None,
) -> bool:
    """Apply document-level restrictions through Frappe's permission hook."""

    del debug
    permission_type = ptype or permission_type
    user = user or frappe.session.user
    roles = set(frappe.get_roles(user))

    if is_system_manager(user):
        return True

    target_employee = doc.get("employee") if hasattr(doc, "get") else None

    if permission_type in READ_PERMISSION_TYPES:
        if ROLE_LOG_EDITOR in roles or ROLE_HR_MANAGER in roles:
            return True

        # Frappe checks read permission before a whitelisted document method is
        # executed. On a brand-new Correction Request, load_employee_context()
        # has not run yet, so employee is still empty. Resolve the signed-in
        # user's active Employee for this permission check instead of denying
        # the unsaved document before it can initialise itself.
        if not target_employee and doc.is_new():
            target_employee = get_employee_for_user(user, active_only=True)

        return bool(target_employee and can_read_employee(target_employee, user))

    active_own_employees = set(get_employees_for_user(user, active_only=True))
    if not active_own_employees:
        return False

    if permission_type == "create":
        # Permission is checked before ``before_insert``. Allow an empty value so
        # the controller can fill the current user's preferred active Employee.
        target_employee = target_employee or get_employee_for_user(user)

    # Reporting managers have read-only access to reportee trackers. Only an
    # active tracker owner may pass normal create/write checks.
    return bool(target_employee and target_employee in active_own_employees)


def _tracker_log_employee(doc) -> str | None:
    """Resolve Employee for historical logs missing the denormalised field."""

    employee = doc.get("employee") if hasattr(doc, "get") else None
    if employee:
        return employee

    tracker = doc.get("time_tracker") if hasattr(doc, "get") else None
    if not tracker:
        return None

    return frappe.db.get_value("Time Tracker", tracker, "employee")


def tracker_log_has_permission(
    doc,
    user: str | None = None,
    ptype: str | None = None,
    debug: bool = False,
    permission_type: str | None = None,
) -> bool:
    """Restrict manual creation and all edits independently from visibility."""

    del debug
    permission_type = ptype or permission_type
    user = user or frappe.session.user

    # Direct/manual creation is reserved for System Manager. Timer users create
    # logs only through the validated Time Tracker API and its private flag.
    if permission_type == "create":
        return is_system_manager(user)

    if permission_type in READ_PERMISSION_TYPES:
        employee = _tracker_log_employee(doc)
        return bool(employee and can_read_employee(employee, user))

    return is_tracker_log_editor(user)


def correction_request_query(user: str | None = None) -> str:
    return _query_condition("Time Tracker Correction Request", user)


def correction_request_has_permission(
    doc,
    user: str | None = None,
    ptype: str | None = None,
    debug: bool = False,
    permission_type: str | None = None,
) -> bool:
    """Limit requests to the employee while allowing managers to review reportees."""

    del debug
    permission_type = ptype or permission_type
    user = user or frappe.session.user

    if is_system_manager(user):
        return True

    target_employee = doc.get("employee") if hasattr(doc, "get") else None
    roles = set(frappe.get_roles(user))

    if permission_type in READ_PERMISSION_TYPES:
        if ROLE_LOG_EDITOR in roles or ROLE_HR_MANAGER in roles:
            return True

        # Frappe checks read permission before a whitelisted document method is
        # executed. On a brand-new Correction Request, load_employee_context()
        # has not run yet, so employee is still empty. Resolve the signed-in
        # user's active Employee for this permission check instead of denying
        # the unsaved document before it can initialise itself.
        if not target_employee and doc.is_new():
            target_employee = get_employee_for_user(user, active_only=True)

        return bool(target_employee and can_read_employee(target_employee, user))

    active_own = set(get_employees_for_user(user, active_only=True))
    if permission_type == "create":
        target_employee = target_employee or get_employee_for_user(user)
        return bool(target_employee and target_employee in active_own)

    if ROLE_LOG_EDITOR in roles or ROLE_HR_MANAGER in roles:
        return True

    state = doc.get("workflow_state") if hasattr(doc, "get") else None
    if target_employee in active_own and state in {None, "Requested"}:
        return True

    return bool(
        ROLE_MANAGER in roles
        and target_employee
        and can_read_employee(target_employee, user)
    )
