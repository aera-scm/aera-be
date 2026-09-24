"""BR-08: atomic reservations against a versioned material/plant balance."""

from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

from services.shared.dynamo import from_item, table_name, to_item


class ReservationConflict(ValueError):
    pass


class Ledger:
    def __init__(self, client: Any, env: str | None = None) -> None:
        self.client = client
        self.table = table_name("ledger", env)

    def _read(self, pk: str, sk: str) -> dict[str, Any] | None:
        item = self.client.get_item(
            TableName=self.table, Key=to_item({"PK": pk, "SK": sk}), ConsistentRead=True
        ).get("Item")
        return from_item(item) if item else None

    def reserve(
        self,
        *,
        material: str,
        plant: str,
        reservation_id: str,
        case_id: str,
        quantity: Decimal,
        available: Decimal,
        source_ref: str,
    ) -> None:
        if not all(v.is_finite() and v >= 0 for v in (quantity, available)) or quantity <= 0:
            raise ValueError("reservation quantities must be finite and positive")
        if quantity != quantity.to_integral_value() or not source_ref.startswith("SAP:"):
            raise ValueError("base-unit quantity and SAP source required")
        pk = f"MAT#{material}#PLANT#{plant}"
        sk = f"RES#{reservation_id}"
        existing = self._read(pk, sk)
        if existing:
            if (
                existing["caseId"] != case_id
                or existing["qty"] != quantity
                or existing["status"] == "RELEASED"
            ):
                raise ReservationConflict("reservation identity or state mismatch")
            return
        balance = self._read(pk, "BAL")
        version = balance["version"] if balance else 0
        allocated = balance["allocated"] if balance else Decimal(0)
        if quantity + allocated > available:
            raise ReservationConflict("insufficient unreserved stock")
        update: dict[str, Any] = {
            "TableName": self.table,
            "Item": to_item(
                {
                    "PK": pk,
                    "SK": "BAL",
                    "allocated": allocated + quantity,
                    "version": version + 1,
                    "sourceRef": source_ref,
                }
            ),
            "ConditionExpression": "version = :version" if balance else "attribute_not_exists(PK)",
        }
        if balance:
            update["ExpressionAttributeValues"] = to_item({":version": version})
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {"Put": update},
                    {
                        "Put": {
                            "TableName": self.table,
                            "Item": to_item(
                                {
                                    "PK": pk,
                                    "SK": sk,
                                    "caseId": case_id,
                                    "qty": quantity,
                                    "status": "HELD",
                                    "version": 1,
                                    "sourceRef": source_ref,
                                }
                            ),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            raise ReservationConflict("stock reservation raced; re-read before retry") from None

    def release(self, *, material: str, plant: str, reservation_id: str) -> None:
        pk = f"MAT#{material}#PLANT#{plant}"
        sk = f"RES#{reservation_id}"
        reservation = self._read(pk, sk)
        if reservation is None or reservation["status"] == "RELEASED":
            return
        balance = self._read(pk, "BAL")
        if balance is None or balance["allocated"] < reservation["qty"]:
            raise ReservationConflict("ledger balance inconsistent")
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.table,
                            "Key": to_item({"PK": pk, "SK": "BAL"}),
                            "UpdateExpression": "SET allocated = :left, version = :next",
                            "ConditionExpression": "version = :version",
                            "ExpressionAttributeValues": to_item(
                                {
                                    ":left": balance["allocated"] - reservation["qty"],
                                    ":next": balance["version"] + 1,
                                    ":version": balance["version"],
                                }
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table,
                            "Key": to_item({"PK": pk, "SK": sk}),
                            "UpdateExpression": "SET #status = :released, version = :next",
                            "ConditionExpression": "version = :version AND #status = :held",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": to_item(
                                {
                                    ":released": "RELEASED",
                                    ":held": "HELD",
                                    ":version": reservation["version"],
                                    ":next": reservation["version"] + 1,
                                }
                            ),
                        }
                    },
                ]
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            raise ReservationConflict("release raced; retry from current balance") from None
