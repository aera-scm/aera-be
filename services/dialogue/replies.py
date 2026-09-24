"""FR-NEG-03 / AT-21: match gated supplier replies and resume waiting cases."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from services.dialogue.thread import DialogueStatus, Thread, accept_reply
from services.shared.cases import CaseStore
from services.shared.dynamo import from_item, table_name
from services.shared.models import CaseStatus, SignalStatus
from services.shared.runtime import emit
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
                self.dynamodb.update_item(
                    TableName=self.table,
                    Key={"PK": raw["PK"], "SK": raw["SK"]},
                    UpdateExpression="SET #s = :replied, replySignalId = :signal",
                    ConditionExpression="#s = :sent AND supplierId = :supplier",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":sent": {"S": "SENT"}, ":replied": {"S": "REPLIED"},
                        ":signal": {"S": signal_id},
                        ":supplier": {"S": signal.supplier_id},
                    },
                )
            except (ValueError, self.dynamodb.exceptions.ConditionalCheckFailedException):
                continue
            emit(
                self.bus, "SupplierReplyMatched",
                {"caseId": signal.case_id, "signalId": signal_id,
                 "messageId": message["messageId"]},
                component="dialogue", case_id=signal.case_id, environment=self.env,
            )
            emit(
                self.bus, "CaseReadyForRun",
                {"caseId": signal.case_id, "reason": "supplier reply", "signalId": signal_id},
                component="dialogue", case_id=signal.case_id, environment=self.env,
            )
            return True
        return False
