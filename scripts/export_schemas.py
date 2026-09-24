"""Export the shared model as one JSON Schema document (SRD 6.5, 7.2).

`make types` feeds it to scripts/generate-types.mjs, which writes the console's
`src/api/types.generated.ts`. The API contract is defined once, in services/shared/models.py.

    uv run --locked python scripts/export_schemas.py > build/aera-contract.schema.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.shared.models import json_schemas  # noqa: E402


def _untitled(node: Any, *, keep: bool = False) -> Any:
    # Property titles become one TypeScript alias each; only model titles are kept.
    if isinstance(node, dict):
        return {
            key: _untitled(value, keep=key in ("definitions",))
            for key, value in node.items()
            if keep or key != "title"
        }
    if isinstance(node, list):
        return [_untitled(value) for value in node]
    return node


def contract() -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    for name, schema in json_schemas().items():
        for nested, body in schema.pop("$defs", {}).items():
            definitions.setdefault(nested, body)
        definitions[name] = schema
    text = json.dumps(definitions).replace("#/$defs/", "#/definitions/")
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "AeraContract",
        "type": "object",
        "definitions": {
            name: {**_untitled(body), "title": name} for name, body in json.loads(text).items()
        },
        "properties": {name: {"$ref": f"#/definitions/{name}"} for name in json_schemas()},
        "additionalProperties": False,
    }


if __name__ == "__main__":
    json.dump(contract(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
