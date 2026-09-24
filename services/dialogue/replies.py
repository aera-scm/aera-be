"""FR-NEG-03 / AT-21: match gated supplier replies and resume waiting cases."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from services.dialogue.thread import DialogueStatus, Thread, accept_reply
from services.shared.cases import CaseStore
from services.shared.dynamo import from_item, table_name, to_item
from services.shared.models import CaseStatus, SignalStatus
from services.shared.signals import SignalStore

TOKEN = re.compile(r"\b[A-F0-9]{24}\b")


@dataclass
class ReplyMatcher:
    dynamodb: Any
    bus: Any
    env: str | None = None

    def __post_init__(self) -> None:
        self.cases = CaseStore(self.dynamodb, self.env)
        self.signals = SignalStore(self.dynamodb, self.env)
        self.table = table_name("dialogue", self.env)

    def match(self, signal_id: str) -> bool:
        signal = self.signals.get(signal_id)
        if (
            signal is None or signal.status is not SignalStatus.ACCEPTED
            or not signal.sender_verified or not signal.case_id or not signal.supplier_id
        ):
            return False
        case = self.cases.get(signal.case_id)
        if case is None or case.status is not CaseStatus.WAITING_SUPPLIER:
            return False
        tokens = TOKEN.findall(signal.normalized_text or "")
        if len(tokens) != 1:
            return False
        token = tokens[0]
        page = self.dynamodb.query(
            TableName=self.table,
            IndexName="GSI1",
            KeyConditionExpression="referenceToken = :token",
            ExpressionAttributeValues={":token": {"S": token}},
        )
        for raw in page.get("Items", []):
            message = from_item(raw)
            if message.get("caseId") != signal.case_id or message.get("status") != "SENT":
                continue
            thread = Thread(
                signal.case_id, str(message["supplierId"]), token,
                datetime.fromisoformat(str(message["sentAt"])),
                datetime.fromisoformat(str(message["remindAt"])),
                datetime.fromisoformat(str(message["deadline"])),
                DialogueStatus.WAITING, bool(message.get("reminderSent")),
            )
            try:
                accept_reply(
                    thread, token=token, supplier_id=signal.supplier_id,
                    signal_id=signal_id, gated=True, now=signal.received_at,
                )
                outbox = table_name("cases", self.env)
                events = (
                    ("SupplierReplyMatched", {"caseId": signal.case_id,
                                              "signalId": signal_id,
                                              "messageId": message["messageId"]}),
                    ("CaseReadyForRun", {"caseId": signal.case_id,
                                         "reason": "supplier reply", "signalId": signal_id}),
                )
                self.dynamodb.transact_write_items(TransactItems=[
                    {"Update": {
                        "TableName": self.table, "Key": {"PK": raw["PK"], "SK": raw["SK"]},
                        "UpdateExpression": "SET #s = :replied, replySignalId = :signal",
                        "ConditionExpression": "#s = :sent AND supplierId = :supplier",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {
                            ":sent": {"S": "SENT"}, ":replied": {"S": "REPLIED"},
                            ":signal": {"S": signal_id},
                            ":supplier": {"S": signal.supplier_id},
                        },
                    }},
                    *[ {"Put": {
                        "TableName": outbox,
                        "Item": to_item({
                            "PK": f"CASE#{signal.case_id}",
                            "SK": f"OUTBOX#SUPPLIER_REPLY#{signal_id}#{kind}",
                            "eventType": kind, "data": data, "sent": False,
                        }),
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }} for kind, data in events],
                ])
            except (ValueError, self.dynamodb.exceptions.TransactionCanceledException):
                continue
            return True
        return False
