"""Read five purchase orders without printing their data (IR-01)."""

import json
import os
import sys
from collections.abc import Mapping

from sap_transport import HOST, PO_PATH, Fetch, SapError, environment_key, get

PATH = PO_PATH


def purchase_order_count(body: bytes) -> int:
    try:
        payload = json.loads(body)
        rows = payload["d"]["results"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 5:
            raise ValueError
        if not all(
            isinstance(row, dict)
            and isinstance(row.get("PurchaseOrder"), str)
            and row["PurchaseOrder"]
            for row in rows
        ):
            raise ValueError
        return len(rows)
    except (ValueError, TypeError, KeyError):
        raise SapError(
            "SAP response has no valid purchase orders in an OData V2 envelope."
        ) from None


def main(*, environ: Mapping[str, str] | None = None, fetch: Fetch = get) -> int:
    try:
        key = environment_key(os.environ if environ is None else environ)
        count = purchase_order_count(fetch(PATH, key, "application/json"))
    except SapError as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps({"status": "pass", "count": count, "sourceRef": f"https://{HOST}{PATH}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
