"""FR-NEG-03/04: deterministic reply matching and one-reminder timeout."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum


class DialogueStatus(StrEnum):
    WAITING = "WAITING"
    ANSWERED = "ANSWERED"
    ESCALATED = "ESCALATED"


class TimeoutAction(StrEnum):
    NONE = "NONE"
    REMIND = "REMIND"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class Thread:
    case_id: str
    supplier_id: str
    reference_token: str
    sent_at: datetime
    remind_at: datetime
    deadline: datetime
    status: DialogueStatus = DialogueStatus.WAITING
    reminder_sent: bool = False
    reply_signal_id: str | None = None


def start(
    case_id: str,
    supplier_id: str,
    token: str,
    sent_at: datetime,
    stockout_at: datetime,
    *,
    timeout: timedelta = timedelta(hours=4),
) -> Thread:
    if (
        not case_id
        or not supplier_id
        or not token
        or sent_at.tzinfo is None
        or stockout_at.tzinfo is None
        or timeout <= timedelta(0)
    ):
        raise ValueError("dialogue requires case, supplier, token and aware times")
    deadline = min(sent_at + timeout, stockout_at - timedelta(minutes=5))
    if deadline <= sent_at:
        raise ValueError("supplier question cannot finish before stock-out")
    return Thread(
        case_id, supplier_id, token, sent_at, sent_at + (deadline - sent_at) / 2, deadline
    )


def tick(thread: Thread, now: datetime) -> tuple[Thread, TimeoutAction]:
    if now.tzinfo is None:
        raise ValueError("timer requires an aware time")
    if thread.status is not DialogueStatus.WAITING:
        return thread, TimeoutAction.NONE
    if now >= thread.deadline:
        return replace(thread, status=DialogueStatus.ESCALATED), TimeoutAction.ESCALATE
    if now >= thread.remind_at and not thread.reminder_sent:
        return replace(thread, reminder_sent=True), TimeoutAction.REMIND
    return thread, TimeoutAction.NONE


def accept_reply(
    thread: Thread,
    *,
    token: str,
    supplier_id: str,
    signal_id: str,
    gated: bool,
    now: datetime,
) -> Thread:
    if (
        thread.status is not DialogueStatus.WAITING
        or now.tzinfo is None
        or now >= thread.deadline
        or not gated
        or token != thread.reference_token
        or supplier_id != thread.supplier_id
        or not signal_id
    ):
        raise ValueError("supplier reply does not match an open gated thread")
    return replace(thread, status=DialogueStatus.ANSWERED, reply_signal_id=signal_id)
