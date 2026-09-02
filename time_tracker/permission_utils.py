from __future__ import annotations

from collections.abc import Iterable

import frappe


READ_OR_SELECT = ("read", "select")


def doctype_exists(doctype: str) -> bool:
    """Return whether a DocType is installed without importing its controller."""

    return bool(doctype and frappe.db.exists("DocType", doctype))


def has_doctype_permission(
    doctype: str,
    permission_types: Iterable[str] = READ_OR_SELECT,
    *,
    user: str | None = None,
) -> bool:
    """Check DocType permission using Frappe 15's public API.

    Frappe 15.101.3 accepts ``throw`` rather than ``raise_exception``. This
    helper intentionally uses neither because a normal permission check is
    non-throwing by default.
    """

    if not doctype_exists(doctype):
        return False

    return any(
        bool(
            frappe.has_permission(
                doctype=doctype,
                ptype=permission_type,
                user=user,
            )
        )
        for permission_type in permission_types
    )


def has_document_permission(
    doctype: str,
    name: str,
    permission_types: Iterable[str] = READ_OR_SELECT,
    *,
    user: str | None = None,
) -> bool:
    """Check both DocType and record-level permission for an existing record."""

    if (
        not name
        or not doctype_exists(doctype)
        or not frappe.db.exists(doctype, name)
    ):
        return False

    doc = frappe.get_doc(doctype, name)

    return any(
        bool(
            frappe.has_permission(
                doctype=doctype,
                ptype=permission_type,
                doc=doc,
                user=user,
            )
        )
        for permission_type in permission_types
    )
