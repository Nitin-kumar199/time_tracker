from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import frappe


_LOG_WRITE_FLAG = "time_tracker_time_tracker_log_write"
_EMPLOYEE_SYNC_FLAG = "time_tracker_employee_tracker_sync"


def _flag_is_set(flag_name: str) -> bool:
    return bool(getattr(frappe.flags, flag_name, False))


@contextmanager
def _temporary_flag(flag_name: str) -> Iterator[None]:
    previous = getattr(frappe.flags, flag_name, False)
    setattr(frappe.flags, flag_name, True)

    try:
        yield
    finally:
        setattr(frappe.flags, flag_name, previous)


def is_time_tracker_log_write() -> bool:
    """Whether the current write was initiated by trusted Time Tracker code."""

    return _flag_is_set(_LOG_WRITE_FLAG)


@contextmanager
def time_tracker_log_write() -> Iterator[None]:
    """Allow one validated server operation to insert or update Tracker Log."""

    with _temporary_flag(_LOG_WRITE_FLAG):
        yield


def is_employee_tracker_sync() -> bool:
    """Whether an explicit trusted server utility is creating a Time Tracker."""

    return _flag_is_set(_EMPLOYEE_SYNC_FLAG)


@contextmanager
def employee_tracker_sync() -> Iterator[None]:
    """Allow an explicit trusted utility to create an Employee Time Tracker."""

    with _temporary_flag(_EMPLOYEE_SYNC_FLAG):
        yield
