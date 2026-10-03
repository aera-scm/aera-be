"""FR-ING-02: suppress MRP reschedule proposals inside the working-day tolerance.

Defaults: reschedule-in below 3 working days and reschedule-out below 15 are suppressed
(config MRP_TOLERANCE_IN_DAYS / MRP_TOLERANCE_OUT_DAYS). Working days are Monday to Friday."""

from __future__ import annotations

from datetime import date, timedelta

IN_DAYS = 3
OUT_DAYS = 15


def working_days_between(a: date, b: date) -> int:
    """Weekdays after the earlier date up to and including the later one."""
    low, high = sorted((a, b))
    return sum(
        1 for n in range(1, (high - low).days + 1) if (low + timedelta(days=n)).weekday() < 5
    )


def mrp_actionable(
    element_date: date,
    rescheduling_date: date | None,
    *,
    in_days: int = IN_DAYS,
    out_days: int = OUT_DAYS,
) -> bool:
    if rescheduling_date is None:
        return True
    shift = working_days_between(element_date, rescheduling_date)
    if rescheduling_date < element_date:
        return shift >= in_days
    if rescheduling_date > element_date:
        return shift >= out_days
    return False
