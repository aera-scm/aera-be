"""AT-28 accounting refuses missing or duplicated signal records."""

from load.ledger import LoadLedger


def test_at_28_ledger_detects_lost_or_duplicated_signals() -> None:
    ledger = LoadLedger()
    ledger.record({"signalId": "one"})
    ledger.record({"signalId": "two"})
    assert ledger.report([{"signalId": "one"}, {"signalId": "two"}], 30)["pass"] is True
    lost = ledger.report([{"signalId": "one"}], 30)
    assert lost["pass"] is False and lost["lostSignalIds"] == ["two"]
    duplicate = ledger.report([{"signalId": "one"}, {"signalId": "one"}, {"signalId": "two"}], 30)
    assert duplicate["pass"] is False and duplicate["duplicateIds"] == ["one"]
    assert ledger.report([{"signalId": "one"}, {"signalId": "two"}], 29)["pass"] is False
