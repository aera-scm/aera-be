"""AT-28 signal accounting across intake and gate states."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LoadLedger:
    submitted: list[str] = field(default_factory=list)

    def record(self, response: dict[str, Any]) -> None:
        signal_id = response.get("signalId")
        if not isinstance(signal_id, str) or not signal_id:
            raise ValueError("signal submission has no ID")
        self.submitted.append(signal_id)

    def report(self, rows: list[dict[str, Any]], active_cases: int) -> dict[str, Any]:
        seen = [str(row.get("signalId")) for row in rows]
        expected = set(self.submitted)
        found = set(seen)
        submitted_rows = [row for row in rows if row.get("signalId") in expected]
        unfinished = sorted(
            str(row["signalId"])
            for row in submitted_rows
            if row.get("status") != "ACCEPTED" or not row.get("caseId") or not row.get("poNumber")
        )
        cases_by_po: dict[str, set[str]] = {}
        for row in submitted_rows:
            if row.get("status") == "ACCEPTED" and row.get("poNumber") and row.get("caseId"):
                cases_by_po.setdefault(str(row["poNumber"]), set()).add(str(row["caseId"]))
        duplicate_cases = {
            po: sorted(case_ids) for po, case_ids in cases_by_po.items() if len(case_ids) > 1
        }
        return {
            "submitted": len(self.submitted),
            "uniqueSubmitted": len(expected),
            "observed": len(expected & found),
            "lostSignalIds": sorted(expected - found),
            "duplicateIds": sorted(
                signal_id for signal_id in expected if seen.count(signal_id) > 1
            ),
            "unfinishedSignalIds": unfinished,
            "duplicateCasesByPo": duplicate_cases,
            "minimumActiveCases": active_cases,
            "pass": (
                len(self.submitted) == len(expected)
                and expected <= found
                and all(seen.count(signal_id) == 1 for signal_id in expected)
                and not unfinished
                and not duplicate_cases
                and active_cases >= 30
            ),
        }
