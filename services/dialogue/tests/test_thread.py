"""AT-21/22: hostile or missing replies cannot bypass supplier dialogue controls."""

from datetime import UTC, datetime, timedelta

import pytest

from services.dialogue.thread import (
    DialogueStatus,
    Thread,
    TimeoutAction,
    accept_reply,
    start,
    tick,
)

NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)


def thread(stockout_hours: int = 6) -> Thread:
    return start(
        "EXC-2026-0914", "1000234", "AERA20260914ABCDEF", NOW,
        NOW + timedelta(hours=stockout_hours),
    )


def test_at_22_one_reminder_then_escalation_before_stockout() -> None:
    initial = thread()
    assert initial.deadline == NOW + timedelta(hours=4)
    waiting, action = tick(initial, NOW + timedelta(hours=2))
    assert action is TimeoutAction.REMIND and waiting.reminder_sent
    same, action = tick(waiting, NOW + timedelta(hours=3))
    assert same == waiting and action is TimeoutAction.NONE
    expired, action = tick(same, NOW + timedelta(hours=4))
    assert action is TimeoutAction.ESCALATE
    assert expired.status is DialogueStatus.ESCALATED
    assert tick(expired, NOW + timedelta(hours=5))[1] is TimeoutAction.NONE


def test_at_22_earlier_stockout_shortens_timeout() -> None:
    initial = thread(2)
    assert initial.deadline == NOW + timedelta(hours=2, minutes=-1)
    assert tick(initial, initial.deadline)[1] is TimeoutAction.ESCALATE
    assert initial.deadline < NOW + timedelta(hours=2)


def test_at_21_only_gated_reply_from_matching_supplier_resumes() -> None:
    initial = thread()
    for token, supplier, gated, received_at in (
        ("other", initial.supplier_id, True, NOW + timedelta(hours=1)),
        (initial.reference_token, "attacker", True, NOW + timedelta(hours=1)),
        (initial.reference_token, initial.supplier_id, False, NOW + timedelta(hours=1)),
        (initial.reference_token, initial.supplier_id, True, NOW + timedelta(hours=4)),
    ):
        with pytest.raises(ValueError, match="open gated thread"):
            accept_reply(
                initial, token=token, supplier_id=supplier,
                signal_id="SIG-1", gated=gated, now=received_at,
            )
    answered = accept_reply(
        initial, token=initial.reference_token, supplier_id=initial.supplier_id,
        signal_id="SIG-1", gated=True, now=NOW + timedelta(hours=1),
    )
    assert answered.status is DialogueStatus.ANSWERED
    assert answered.reply_signal_id == "SIG-1"
    assert tick(answered, NOW + timedelta(hours=5))[1] is TimeoutAction.NONE


def test_fr_neg_04_refuses_question_when_stockout_is_imminent() -> None:
    with pytest.raises(ValueError, match="before stock-out"):
        start("case", "supplier", "token", NOW, NOW + timedelta(seconds=30))
