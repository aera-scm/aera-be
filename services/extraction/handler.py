"""Extraction: fields and confidence from documents and photos, language, field status
(SRD 6.25.1 step 3, FR-ING-06, FR-ING-07, BR-02, IR-04).

On `SignalAccepted`:

1. Each PDF or image attachment goes to Amazon Textract AnalyzeDocument with QUERIES for
   the critical fields; each answer keeps Textract's confidence.
2. PO and material numbers typed in the message text are taken literally.
3. Every critical field is checked against the SAP purchase order (BR-02): an exact match
   is SAP_MATCHED, else confidence >= CRITICAL_FIELD_MIN_CONF is CONFIRMED, else
   UNCONFIRMED — which no calculation may use until a planner confirms it.
4. Text read from images is scanned by the prompt-attack guardrail too, since the gatekeeper
   could not see it before OCR.
5. Amazon Comprehend names the language.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from services.extraction.multilingual import BedrockLocator, Word, verified_spans
from services.gatekeeper.handler import Guardrail
from services.rules.br_02 import MIN_CONFIDENCE, field_status
from services.shared.audit import AuditWriter
from services.shared.models import ExtractedField, Signal, SignalChannel, SignalStatus
from services.shared.quarantine import quarantine
from services.shared.runtime import emit
from services.shared.sap_client import SapClient, SapError, SapNotFoundError
from services.shared.sap_values import edm_date, results
from services.shared.signals import SignalStore

COMPONENT = "extraction"
PO_SERVICE = "API_PURCHASEORDER_PROCESS_SRV"
DOCUMENT_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")
QUERIES = (
    ("PO_NUMBER", "What is the purchase order number?"),
    ("MATERIAL", "What is the material or part number?"),
    ("QUANTITY", "What quantity is shipped or confirmed?"),
    ("DELIVERY_DATE", "What is the delivery date?"),
    ("PRICE", "What is the unit price?"),
)
_PO = re.compile(r"\b(45\d{8})\b")
_MATERIAL = re.compile(r"\b(MAT-\d{5})\b")
_ETA = re.compile(r"\bETA (\d{4}-\d{2}-\d{2}T[0-9:.]+Z?)")
_TRACKING = re.compile(r"\btracking ([A-Z0-9-]{4,40})")
_STATUS = re.compile(r"^Carrier status ([A-Z_]{3,30})")


@dataclass(frozen=True)
class Reading:
    name: str
    value: str
    confidence: float


def textract_readings(client: Any, bucket: str, key: str) -> tuple[list[Reading], str]:
    """Query answers with confidence (0-1) and the page text of one document."""
    readings, text, _ = textract_document(client, bucket, key)
    return readings, text


def textract_document(client: Any, bucket: str, key: str) -> tuple[list[Reading], str, list[Word]]:
    """Keep query answers and word confidence for language-specific extraction."""
    response = client.analyze_document(
        Document={"S3Object": {"Bucket": bucket, "Name": key}},
        FeatureTypes=["QUERIES"],
        QueriesConfig={"Queries": [{"Text": text, "Alias": alias} for alias, text in QUERIES]},
    )
    blocks = {block["Id"]: block for block in response.get("Blocks", [])}
    readings: list[Reading] = []
    lines: list[str] = []
    words: list[Word] = []
    for block in blocks.values():
        if block["BlockType"] == "LINE" and block.get("Text"):
            lines.append(str(block["Text"]))
        if block["BlockType"] == "WORD" and block.get("Text"):
            words.append(Word(str(block["Text"]), float(block.get("Confidence", 0)) / 100))
        if block["BlockType"] != "QUERY":
            continue
        alias = block.get("Query", {}).get("Alias")
        answers = [
            blocks[child]
            for relation in block.get("Relationships", [])
            if relation.get("Type") == "ANSWER"
            for child in relation.get("Ids", [])
            if child in blocks
        ]
        if alias and answers:
            best = max(answers, key=lambda b: float(b.get("Confidence", 0)))
            readings.append(
                Reading(str(alias), str(best["Text"]).strip(), float(best["Confidence"]) / 100)
            )
    return readings, "\n".join(lines), words


def text_readings(text: str) -> list[Reading]:
    readings = [Reading("PO_NUMBER", po, 1.0) for po in dict.fromkeys(_PO.findall(text))]
    readings += [Reading("MATERIAL", m, 1.0) for m in dict.fromkeys(_MATERIAL.findall(text))]
    return readings


def carrier_readings(text: str) -> list[Reading]:
    """Structured carrier event fields (the webhook wrote them as `ETA ...; tracking ...`)."""
    readings = []
    for name, pattern in (
        ("ETA", _ETA),
        ("TRACKING_NUMBER", _TRACKING),
        ("CARRIER_STATUS", _STATUS),
    ):
        match = pattern.search(text)
        if match:
            readings.append(Reading(name, match.group(1), 1.0))
    return readings


def sap_values(sap: SapClient, po_number: str, material: str | None) -> dict[str, str]:
    """The SAP values each critical field is compared with (BR-01: SAP is the truth)."""
    try:
        record = sap.get(
            PO_SERVICE,
            "A_PurchaseOrder",
            {"PurchaseOrder": po_number},
            expand="to_PurchaseOrderItem/to_ScheduleLine",
        )
    except SapNotFoundError:
        return {}
    items = results(record.data.get("to_PurchaseOrderItem"))
    chosen = [i for i in items if material and i.get("Material") == material] or items[:1]
    values = {"PO_NUMBER": po_number}
    if len(chosen) == 1:
        item = chosen[0]
        values["MATERIAL"] = str(item.get("Material", ""))
        values["QUANTITY"] = str(item.get("OrderQuantity", ""))
        values["PRICE"] = str(item.get("NetPriceAmount", ""))
        lines = results(item.get("to_ScheduleLine"))
        delivery = edm_date(lines[0].get("ScheduleLineDeliveryDate")) if lines else None
        if delivery is not None:
            values["DELIVERY_DATE"] = delivery.isoformat()
    return values


@dataclass
class Extraction:
    signals: SignalStore
    textract: Any
    comprehend: Any
    bucket: str
    sap: SapClient
    guardrail: Guardrail
    audit: AuditWriter
    bus: Any
    min_confidence: Callable[[], float] = lambda: MIN_CONFIDENCE
    locator: Callable[[str, str], dict[str, str]] | None = None
    env: str | None = None

    def handle(self, signal_id: str) -> Signal | None:
        signal = self.signals.get(signal_id)
        if signal is None or signal.status is not SignalStatus.ACCEPTED or signal.fields:
            return signal
        readings = text_readings(signal.normalized_text or "")
        if signal.channel is SignalChannel.CARRIER:
            readings += carrier_readings(signal.normalized_text or "")
        documents: list[tuple[list[Reading], str, list[Word]]] = []
        for key in signal.attachments:
            if key.lower().endswith(DOCUMENT_SUFFIXES):
                documents.append(textract_document(self.textract, self.bucket, key))
        scanned = "\n".join(text for _, text, _ in documents if text)
        if scanned:
            scan = self.guardrail.scan(scanned)
            if scan.blocked:
                return quarantine(
                    signal,
                    f"{scan.reason} (in text read from an attachment)",
                    signals=self.signals,
                    audit=self.audit,
                    bus=self.bus,
                    component=COMPONENT,
                    guardrail="BLOCKED",
                    env=self.env,
                )
        language = self._language("\n".join([signal.normalized_text or "", scanned]))
        for queries, text, words in documents:
            if language == "en":
                readings += queries
            elif language in {"id", "de"} and self.locator is not None:
                readings += [
                    Reading(field.name, field.value, field.confidence)
                    for field in verified_spans(self.locator(text, language), words, text)
                ]
        po = signal.po_number or next((r.value for r in readings if r.name == "PO_NUMBER"), None)
        material = signal.material or next(
            (r.value for r in readings if r.name == "MATERIAL"), None
        )
        truth: dict[str, str] = {}
        if po:
            try:
                truth = sap_values(self.sap, po, material)
            except SapError:
                truth = {}  # SAP unreachable: nothing matches, confidence alone decides
        threshold = self.min_confidence()
        fields = [
            ExtractedField(
                field_id=f"{signal.signal_id}-{index:02d}",
                signal_id=signal.signal_id,
                name=reading.name,  # type: ignore[arg-type]
                value=reading.value,
                confidence=reading.confidence,
                status=field_status(
                    reading.name,
                    reading.value,
                    reading.confidence,
                    sap_value=truth.get(reading.name),
                    min_confidence=threshold,
                ),
            )
            for index, reading in enumerate(readings, start=1)
        ]
        extracted = signal.model_copy(
            update={
                "fields": fields,
                "po_number": po,
                "material": material or truth.get("MATERIAL") or None,
                "language": language,
            }
        )
        self.signals.save(extracted)
        emit(
            self.bus,
            "SignalExtracted",
            {
                "signalId": signal.signal_id,
                "poNumber": extracted.po_number,
                "material": extracted.material,
                "language": language,
                "fields": [
                    {
                        "fieldId": f.field_id,
                        "name": f.name,
                        "value": f.value,
                        "confidence": f.confidence,
                        "status": f.status.value,
                    }
                    for f in fields
                ],
            },
            component=COMPONENT,
            environment=self.env,
        )
        return extracted

    def _language(self, text: str) -> str | None:
        if not text.strip():
            return None
        languages = self.comprehend.detect_dominant_language(Text=text[:4500]).get("Languages", [])
        best = max(languages, key=lambda item: item.get("Score", 0), default=None)
        return str(best["LanguageCode"]) if best and best.get("Score", 0) >= 0.5 else None


_extraction: Extraction | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _extraction
    if _extraction is None:
        from services.shared import runtime
        from services.shared.config import Config

        dynamodb = runtime.client("dynamodb")
        config = Config(dynamodb)
        _extraction = Extraction(
            signals=SignalStore(dynamodb),
            textract=runtime.client("textract"),
            comprehend=runtime.client("comprehend"),
            bucket=runtime.raw_bucket(),
            sap=runtime.sap_client(),
            guardrail=Guardrail(
                runtime.client("bedrock-runtime"),
                lambda: runtime.parameter("GUARDRAIL_ID"),
                lambda: runtime.parameter("GUARDRAIL_VERSION"),
            ),
            audit=AuditWriter(dynamodb),
            bus=runtime.client("events"),
            min_confidence=lambda: float(config.decimal("CRITICAL_FIELD_MIN_CONF")),
            locator=lambda text, language: BedrockLocator(
                runtime.client("bedrock-runtime"), runtime.parameter("MODEL_SMALL_ID")
            )(text, language),
        )
    signal = _extraction.handle(str(event["detail"]["data"]["signalId"]))
    return {"status": None if signal is None else signal.status.value}
