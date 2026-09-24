"""Create fixed German PDF and Indonesian photo evaluation cases."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

CASES = Path(__file__).resolve().parent / "cases"


def main() -> None:
    base = json.loads((CASES / "lpc-02.json").read_text(encoding="utf-8"))
    for number in range(1, 16):
        language = "de" if number % 2 else "id"
        quantity = 600 + number * 10
        case = deepcopy(base)
        case.update(
            {
                "id": f"ml-{number:02d}",
                "category": "MULTILINGUAL",
                "subsets": ["ml"],
                "description": (
                    f"{'German PDF' if language == 'de' else 'Indonesian WhatsApp photo'}: "
                    f"{quantity} units read at 71% and kept unconfirmed."
                ),
                "multilingualLanguage": language,
                "multilingualQuantity": quantity,
                "signals": ["eval-multilingual"],
            }
        )
        case["plan"] = {
            "options": [o for o in base["plan"]["options"] if o["id"] in {"B", "C"}],
            "chosen": ["C"],
            "rationale": "SAP-backed donor transfer",
        }
        case["truth"] = {
            "accepted": ["eval-multilingual"],
            "extracted": {
                "language": language,
                "QUANTITY": f"{quantity} {'Stueck' if language == 'de' else 'unit'}:UNCONFIRMED",
            },
            "optionCosts": {"B": 51900, "C": 4100},
            "blocked": {"B": ["V-06"]},
            "tier": 1,
            "status": "AUTO_APPROVED",
            "acceptable": [["STO"]],
        }
        (CASES / f"ml-{number:02d}.json").write_text(
            json.dumps(case, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
