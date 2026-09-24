"""Evaluation runner (SRD 8.3, 8.4; WP-13).

Each case in `eval/cases/*.json` holds changes to the Mirror seed, the signals to deliver,
the plan a planner proposes, and the ground truth. For every case the runner resets the
Mirror and applies the changes, starts fresh AWS fakes, delivers the signals through the
real pipeline (intake, gatekeeper, extraction, case service), calls the real tools, runs
the Verifier and routing, and scores the outcome deterministically.

Offline (the default, and what CI runs) the case's `plan` stands in for the agent's
choices, and the Guardrail stand-ins of the pipeline tests replace Bedrock. What depends on
the model itself (the model's own plan choice, tool-call accuracy, time to plan, cost per
case) is reported as not measured until the runner is pointed at a deployed agent.

    uv run --locked python eval/runner.py --subset ci --out eval/reports
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

ROOT = Path(__file__).resolve().parents[1]
CASES = Path(__file__).resolve().parent / "cases"
ENV = "eval"
T0 = "2026-10-05T08:00:00Z"
RAW_BUCKET = "aera-eval-raw"
APP_SECRET = "synthetic-app-secret"  # pragma: allowlist secret
CARRIER_KEY = "synthetic-carrier-key"  # pragma: allowlist secret
PLANNER = "planner-eval"

Category = Literal[
    "LATE_PO_CLEAR",
    "LATE_PO_CONFLICT",
    "MATERIAL_SHORTAGE",
    "CARRIER_DELAY",
    "LOW_CONFIDENCE",
    "MULTILINGUAL",
    "ADVERSARIAL",
    "NO_VIABLE_OPTION",
]


def _paths() -> None:
    """The runner uses the repository's services, generators and offline stand-ins."""
    for extra in (ROOT, ROOT / "scripts", ROOT / "tests"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))


# Case format ------------------------------------------------------------------------------


class Spec(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class MirrorChange(Spec):
    entity: str
    where: dict[str, str] = Field(default_factory=dict)
    set: dict[str, str] | None = None
    insert: dict[str, str] | None = None
    remove: bool = False


class FieldRef(Spec):
    """A field of a delivered signal: the synthetic item id and the field name."""

    signal: str
    field: str


class OptionSpec(Spec):
    id: str
    name: str
    action_type: Literal["STO", "AIR_FREIGHT", "ALTERNATE_SUPPLIER"]
    params: dict[str, Any] = Field(default_factory=dict)
    qty_field: FieldRef | None = None


class PlanSpec(Spec):
    confirm: list[FieldRef] = Field(default_factory=list)
    recovery: FieldRef | None = None
    options: list[OptionSpec]
    chosen: list[str]
    rationale: str


class Truth(Spec):
    quarantined: list[str] = Field(default_factory=list)
    accepted: list[str] = Field(default_factory=list)
    figures: dict[str, Decimal] = Field(default_factory=dict)
    option_costs: dict[str, Decimal] = Field(default_factory=dict)
    refused: list[str] = Field(default_factory=list)
    blocked: dict[str, list[str]] = Field(default_factory=dict)
    discrepancies: list[str] = Field(default_factory=list)
    tier: int | None = None
    reason: str | None = None
    status: str | None = None
    acceptable: list[list[str]] = Field(default_factory=list)
    released: int | None = None  # PlanApproved events routing may release at once
    extracted: dict[str, str] = Field(default_factory=dict)


class EvalCase(Spec):
    id: str
    category: Category
    subsets: list[str] = Field(default_factory=list)
    description: str
    case_id: str = "EXC-2026-0914"
    mirror: list[MirrorChange] = Field(default_factory=list)
    config: dict[str, str] = Field(default_factory=dict)
    signals: list[str] = Field(default_factory=list)
    plan: PlanSpec | None = None
    truth: Truth
    multilingual_language: Literal["de", "id"] | None = None
    multilingual_quantity: int | None = None


def load_cases(subset: str | None = None, directory: Path = CASES) -> list[EvalCase]:
    cases = [
        EvalCase.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    return [c for c in cases if subset is None or subset in c.subsets]


# Scoring --------------------------------------------------------------------------------


@dataclass
class Check:
    kind: str
    name: str
    expected: Any
    actual: Any

    @property
    def ok(self) -> bool:
        return bool(self.expected == self.actual)


@dataclass
class CaseResult:
    case: EvalCase
    checks: list[Check] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.ok for c in self.checks)

    def expect(self, kind: str, name: str, expected: Any, actual: Any) -> None:
        self.checks.append(Check(kind, name, expected, actual))


def _share(results: list[CaseResult], kinds: set[str]) -> tuple[int, int]:
    checks = [c for r in results for c in r.checks if c.kind in kinds]
    return sum(1 for c in checks if c.ok), len(checks)


def metrics(results: list[CaseResult]) -> dict[str, Any]:
    """SRD 8.4, deterministic part. Each value is (passed, total)."""
    adversarial = [r for r in results if r.case.category == "ADVERSARIAL"]
    return {
        "figureAccuracy": _share(results, {"figure", "cost"}),
        "tierAccuracy": _share(results, {"tier"}),
        "checkAccuracy": _share(results, {"blocked", "refused", "reason", "status"}),
        "gateAccuracy": _share(results, {"gate"}),
        "extractionAccuracy": _share(results, {"extracted"}),
        "planAcceptability": _share(results, {"acceptable"}),
        "adversarialContainment": (sum(1 for r in adversarial if r.passed), len(adversarial)),
        "casesPassed": (sum(1 for r in results if r.passed), len(results)),
    }


def portfolio_results() -> list[dict[str, Any]]:
    """Score the independently tabulated 20 shared-capacity optima."""
    from services.optimizer.solver import Candidate, Capacity, Need, solve
    from services.optimizer.tests.test_known_portfolios import NOW, PORTFOLIOS

    rows = []
    for name, resource, values, costs, capacity, late, selected, optimum in PORTFOLIOS:
        needs = [
            Need(f"{name}-{i}", f"need-{i}", NOW + timedelta(hours=8), 100,
                 value, "SAP:ORDER")
            for i, value in enumerate(values)
        ]
        actions = [
            Candidate(f"{name}-{i}", f"action-{i}",
                      NOW + timedelta(hours=9 if i in late else 5), 100, 0,
                      cost, (resource,), "ratecard:TEST")
            for i, cost in enumerate(costs)
        ]
        answer = solve(needs, actions, [Capacity(resource, capacity, "SAP:CAPACITY")])
        chosen = tuple(int(a.candidate_id[-1]) for a in answer.allocations)
        deviation = (
            abs(answer.objective_cents - optimum) / optimum
            if answer.objective_cents is not None and optimum else None
        )
        rows.append({
            "id": name, "status": answer.status, "objectiveCents": answer.objective_cents,
            "knownOptimumCents": optimum, "selectedCases": chosen,
            "expectedCases": selected, "deviation": deviation,
            "passed": answer.status in {"OPTIMAL", "FEASIBLE"}
            and deviation is not None and deviation <= 0.01 and chosen == selected,
        })
    return rows


# One case -------------------------------------------------------------------------------


def mirror_admin(url: str, path: str, body: dict[str, Any]) -> None:
    import httpx

    with httpx.Client(base_url=url, timeout=30) as http:
        token = http.get(
            "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/", headers={"x-csrf-token": "Fetch"}
        )
        response = http.post(
            path, json=body, headers={"x-csrf-token": token.headers["x-csrf-token"]}
        )
        if response.status_code != 200:
            raise RuntimeError(f"Mirror {path}: {response.status_code} {response.text}")


@contextmanager
def aws_fakes() -> Iterator[None]:
    from moto import mock_aws

    names = ("AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AERA_ENV")
    saved = {name: os.environ.get(name) for name in names}
    os.environ.update(
        {
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",  # pragma: allowlist secret
            "AERA_ENV": ENV,
        }
    )
    try:
        with mock_aws():
            yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _sign(key: str, message: bytes) -> str:
    import hashlib
    import hmac

    return "sha256=" + hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


class Run:
    """The deployed components wired together offline for one case."""

    def __init__(self, case: EvalCase, mirror_url: str, t0: datetime) -> None:
        import boto3
        from generate_signals import build
        from multilingual import Language as RecordedLanguage
        from multilingual import Ocr as RecordedOcr
        from multilingual import locate, signal
        from seed_config import approver_items, config_items, rate_card_items
        from standins import Guard, Language, Ocr

        from services.case_service.handler import CaseService
        from services.conftest import RecordingBus, create_tables
        from services.extraction.handler import Extraction
        from services.gatekeeper.handler import Gatekeeper, Guardrail
        from services.shared.audit import AuditWriter
        from services.shared.cases import CaseStore
        from services.shared.intake import Intake
        from services.shared.partners import load_contacts
        from services.shared.sap_client import Endpoint, SapClient, Target
        from services.shared.signals import RawStore, SignalStore
        from services.webhooks.handler import Webhooks

        self.case = case
        self.t0 = t0
        self.dynamodb = create_tables(boto3.client("dynamodb", region_name="us-east-1"), ENV)
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=RAW_BUCKET)
        self.bus = RecordingBus()
        stamp = t0.isoformat()
        items = config_items(stamp) + rate_card_items(stamp) + approver_items(stamp)
        for item in items:
            key = item["PK"]["S"].removeprefix("CFG#")
            if key in case.config:
                kind = "N" if "N" in item["value"] else "S"
                item["value"] = {kind: case.config[key]}
            self.dynamodb.put_item(TableName=f"aera-{ENV}-config", Item=item)
        CaseStore(self.dynamodb, ENV).seed_counter(t0.year, 913)
        endpoint = Endpoint(base_url=mirror_url, target=Target.MIRROR, auth=None)
        self.sap = SapClient(read=endpoint, write=None)
        self.items = {item.id: item for item in build(t0)}
        if case.multilingual_language is not None:
            if case.multilingual_quantity is None or case.multilingual_quantity <= 0:
                raise ValueError("multilingual quantity must be positive")
            multilingual_item = signal(case.multilingual_language, case.multilingual_quantity, t0)
            self.items[multilingual_item.id] = multilingual_item
        media = {
            name.split("/")[1].removesuffix(".png"): content
            for item in self.items.values()
            for name, content in item.files.items()
            if name.startswith("media/")
        }

        class Media:
            def fetch(self, media_id: str) -> tuple[bytes, str]:
                return media[media_id], "image/png"

        self.signals = SignalStore(self.dynamodb, ENV)
        raw = RawStore(s3, RAW_BUCKET)
        audit = AuditWriter(self.dynamodb, ENV)
        guard = Guardrail(Guard(), lambda: "g", lambda: "1")
        contacts = load_contacts(self.sap)
        self.intake = Intake(
            dynamodb=self.dynamodb, raw=raw, bus=self.bus, component="eval", env=ENV
        )
        self.hooks = Webhooks(
            intake=self.intake,
            whatsapp=lambda: {"appSecret": APP_SECRET},
            carrier_keys=lambda: {"1000950": CARRIER_KEY},
            media=Media(),
            clock=lambda: t0.timestamp(),
        )
        self.gatekeeper = Gatekeeper(
            signals=self.signals,
            raw=raw,
            contacts=lambda: contacts,
            guardrail=guard,
            audit=audit,
            bus=self.bus,
            env=ENV,
        )
        self.extraction = Extraction(
            signals=self.signals,
            textract=(RecordedOcr(case.multilingual_quantity, case.multilingual_language)
                      if case.multilingual_language is not None
                      and case.multilingual_quantity is not None else Ocr()),
            comprehend=(RecordedLanguage() if case.multilingual_language is not None
                        else Language()),
            bucket=RAW_BUCKET,
            sap=self.sap,
            guardrail=guard,
            audit=audit,
            bus=self.bus,
            locator=locate if case.multilingual_language is not None else None,
            env=ENV,
        )
        self.cases = CaseService(
            dynamodb=self.dynamodb, sap=self.sap, bus=self.bus, clock=lambda: t0, env=ENV
        )
        self.store = CaseStore(self.dynamodb, ENV, clock=lambda: t0)
        self.delivered: dict[str, list[str]] = {}

    def open_cases(self) -> list[str]:
        from services.mrp_poller.handler import MrpPoller

        MrpPoller(sap=self.sap, bus=self.bus, env=ENV).poll()
        [polled] = self.bus.details("MrpExceptionsPolled")
        return list(self.cases.on_mrp(polled["data"]))

    def deliver(self, item_id: str) -> None:
        from services.ses_inbound.handler import to_inbound

        item = self.items[item_id]
        before = len(self.bus.details("SignalReceived"))
        content = next(v for k, v in item.files.items() if not k.startswith("media/"))
        if item.channel == "EMAIL":
            self.intake.receive(to_inbound(content))
        elif item.channel == "WHATSAPP":
            self.hooks.handle(
                {
                    "httpMethod": "POST",
                    "resource": "/webhooks/whatsapp",
                    "headers": {"X-Hub-Signature-256": _sign(APP_SECRET, content)},
                    "body": content.decode(),
                }
            )
        else:
            stamp = str(int(self.t0.timestamp()))
            self.hooks.handle(
                {
                    "httpMethod": "POST",
                    "resource": "/webhooks/carrier",
                    "headers": {
                        "X-Aera-Timestamp": stamp,
                        "X-Aera-Signature": _sign(CARRIER_KEY, stamp.encode() + b"." + content),
                    },
                    "body": content.decode(),
                }
            )
        ids = [e["data"]["signalId"] for e in self.bus.details("SignalReceived")[before:]]
        for signal_id in ids:
            self.gatekeeper.handle(signal_id)
            self.extraction.handle(signal_id)
            self.cases.on_signal(signal_id)
        self.delivered[item_id] = ids

    def field(self, ref: FieldRef) -> Any:
        for signal_id in self.delivered.get(ref.signal, []):
            signal = self.signals.get(signal_id)
            for extracted in signal.fields if signal else []:
                if extracted.name == ref.field:
                    return signal, extracted
        raise LookupError(f"{ref.signal} has no field {ref.field}")

    def confirm(self, ref: FieldRef) -> None:
        """What `POST /cases/{id}/fields/{fieldId}/confirm` records (BR-02)."""
        from services.shared.models import FieldStatus

        signal, extracted = self.field(ref)
        confirmed = extracted.model_copy(
            update={"status": FieldStatus.CONFIRMED, "confirmed_by": f"user:{PLANNER}"}
        )
        fields = [confirmed if f.field_id == extracted.field_id else f for f in signal.fields]
        self.signals.save(signal.model_copy(update={"fields": fields}))


def run_case(case: EvalCase, mirror_url: str, t0: datetime) -> CaseResult:
    started = time.monotonic()
    result = CaseResult(case)
    try:
        mirror_admin(mirror_url, "/admin/reset", {"t0": t0.isoformat().replace("+00:00", "Z")})
        if case.mirror:
            changes = [c.model_dump(exclude_none=True, exclude_defaults=True) for c in case.mirror]
            mirror_admin(mirror_url, "/admin/patch", {"changes": json.dumps(changes)})
        with aws_fakes():
            _score(Run(case, mirror_url, t0), result)
    except Exception as error:  # noqa: BLE001 - a crashing case is a failed case, reported
        result.error = f"{type(error).__name__}: {error}"
    result.seconds = time.monotonic() - started
    return result


def _score(run: Run, result: CaseResult) -> None:
    from services.routing.store import ControlStore
    from services.shared.models import SignalStatus

    case, truth = run.case, run.case.truth
    opened = run.open_cases()
    for item_id in case.signals:
        run.deliver(item_id)
    for item_id in truth.quarantined + truth.accepted:
        statuses = [
            (s.status, s.case_id)
            for s in (run.signals.get(i) for i in run.delivered.get(item_id, []))
            if s is not None
        ]
        expected = SignalStatus.QUARANTINED if item_id in truth.quarantined else "ACCEPTED"
        actual = statuses[0][0] if statuses else None
        result.expect("gate", item_id, str(expected), str(actual))
        if item_id in truth.quarantined:
            result.expect("gate", f"{item_id} joined no case", None, statuses[0][1])
    for name, expected in truth.extracted.items():
        extracted_signal = next(
            (run.signals.get(i) for i in run.delivered.get("eval-multilingual", [])), None
        )
        extracted_actual: str | None = None
        if extracted_signal is not None:
            if name == "language":
                extracted_actual = extracted_signal.language
            else:
                extracted_actual = next(
                    (f"{f.value}:{f.status.value}" for f in extracted_signal.fields
                     if f.name == name),
                    None,
                )
        result.expect("extracted", name, expected, extracted_actual)
    if truth.quarantined:
        result.expect(
            "gate", "no case opened by signals", len(opened), run.bus.types().count("CaseOpened")
        )
    if case.plan is None:
        return
    _plan(run, case.plan, result)
    control = ControlStore(run.dynamodb, ENV)
    route = control.get(case.case_id, f"ROUTE#{_version(run)}") or {}
    if truth.tier is not None:
        result.expect("tier", "tier", truth.tier, route.get("tier"))
    if truth.reason is not None:
        result.expect("reason", "reason", truth.reason, route.get("reason"))
    record = run.store.get(case.case_id)
    if truth.status is not None:
        result.expect("status", "status", truth.status, record.status.value if record else None)
    if truth.released is not None:
        released = sum(1 for e in _outbox(run, case.case_id) if e == "PlanApproved")
        result.expect("status", "released parts", truth.released, released)
    plan = control.get(case.case_id, f"PLAN#{_version(run)}") or {}
    failed: dict[str, list[str]] = {}
    for check in plan.get("checks", []):
        if check["blocking"] and not check["passed"] and check.get("optionId"):
            failed.setdefault(check["optionId"], []).append(check["checkId"])
    result.expect(
        "blocked",
        "failed checks",
        {k: sorted(v) for k, v in truth.blocked.items()},
        {k: sorted(v) for k, v in failed.items()},
    )
    if truth.acceptable:
        types = {o.id: o.action_type for o in case.plan.options}
        chosen = sorted(types[c] for c in case.plan.chosen)
        result.expect(
            "acceptable", "chosen actions", True, chosen in [sorted(a) for a in truth.acceptable]
        )


def _version(run: Run) -> int:
    record = run.store.get(run.case.case_id)
    return record.plan_version if record else 0


def _outbox(run: Run, case_id: str) -> list[str]:
    rows = run.dynamodb.query(
        TableName=f"aera-{ENV}-cases",
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={":pk": {"S": f"CASE#{case_id}"}, ":sk": {"S": "OUTBOX#"}},
    )["Items"]
    return [str(r["eventType"]["S"]) for r in rows]


def _plan(run: Run, spec: PlanSpec, result: CaseResult) -> None:
    """The planner's (offline: the case's) choices, through the real tools and Verifier."""
    from services.shared.models import CaseStatus
    from services.tools import calc, case_tools
    from services.tools.context import ToolContext, ToolError
    from services.verifier.logic import Grounding
    from services.verifier.service import VerifierService

    case, truth = run.case, run.case.truth
    run.store.transition(case.case_id, CaseStatus.INVESTIGATING, actor="system")
    ctx = ToolContext(
        sap=run.sap,
        dynamodb=run.dynamodb,
        bus=run.bus,
        clock=lambda: run.t0,
        env=ENV,
        run_id="eval",
    )
    for ref in spec.confirm:
        run.confirm(ref)
    recovery_at = recovery_ref = None
    if spec.recovery is not None:
        signal, extracted = run.field(spec.recovery)
        recovery_at = str(extracted.value)
        recovery_ref = f"signal:{signal.signal_id}/{extracted.name}"
    impact = calc.calc_impact(ctx, case.case_id, recovery_at, recovery_ref)
    for name, expected in truth.figures.items():
        actual = impact.get(name)
        result.expect("figure", name, expected, None if actual is None else Decimal(str(actual)))
    if truth.discrepancies:
        found = sorted(d["name"] for d in impact["discrepancies"] if d["used"] == "SAP")
        result.expect("figure", "discrepancies (SAP used)", sorted(truth.discrepancies), found)
    options: list[dict[str, Any]] = []
    refused: list[str] = []
    for option in spec.options:
        params = dict(option.params)
        if option.qty_field is not None:
            params["qtyFieldId"] = run.field(option.qty_field)[1].field_id
        try:
            draft = calc.calc_option(ctx, case.case_id, option.action_type, params)
        except ToolError:
            refused.append(option.id)
            continue
        options.append(
            {
                "id": option.id,
                "name": option.name,
                "actions": draft["actions"],
                "coverageUnits": draft["coverageUnits"],
                "arrival": draft["arrival"],
                "costUsd": draft["costUsd"],
                "costSourceRef": draft["costSourceRef"],
                "figures": draft["figures"],
                "rationale": option.name,
            }
        )
    result.expect("refused", "options refused by the tools", sorted(truth.refused), sorted(refused))
    costs = {o["id"]: Decimal(str(o["costUsd"])) for o in options}
    for option_id, cost in truth.option_costs.items():
        result.expect("cost", f"option {option_id} cost", cost, costs.get(option_id))
    proposed = case_tools.propose_plan(
        ctx,
        case.case_id,
        {"options": options, "chosen": spec.chosen, "rationale": spec.rationale},
    )
    if not proposed.get("accepted"):
        raise RuntimeError(f"propose_plan refused: {proposed.get('errors')}")
    VerifierService(
        dynamodb=run.dynamodb,
        sap=run.sap,
        bus=run.bus,
        # Contextual grounding needs the deployed Guardrail; offline it is a fixed pass.
        grounding=lambda rationale, source, query: Grounding(Decimal("0.9"), Decimal("0.9"), True),
        clock=lambda: run.t0,
        env=ENV,
    ).handle(case.case_id, int(proposed["planVersion"]))


# Report ---------------------------------------------------------------------------------


def _pct(pair: tuple[int, int]) -> str:
    passed, total = pair
    return "n/a" if total == 0 else f"{100 * passed / total:.1f}% ({passed}/{total})"


def report(
    results: list[CaseResult], subset: str | None,
    portfolios: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    values = metrics(results)
    portfolio_score = (
        (sum(p["passed"] for p in portfolios), len(portfolios)) if portfolios else (0, 0)
    )
    data: dict[str, Any] = {
        "subset": subset or "all",
        "mode": "offline (scripted planner, Guardrail stand-ins)",
        "limitations": (
            ["Reference-seed variants; not proven held out before prompt tuning",
             "Scripted plan choices and recorded cloud-service stand-ins",
             "Live agent, dialogue, channel and Lab targets not measured"]
            if subset is None else []
        ),
        "metrics": {k: list(v) for k, v in values.items()},
        "cases": [
            {
                "id": r.case.id,
                "category": r.case.category,
                "passed": r.passed,
                "seconds": round(r.seconds, 2),
                "error": r.error,
                "failures": [
                    {
                        "kind": c.kind,
                        "name": c.name,
                        "expected": str(c.expected),
                        "actual": str(c.actual),
                    }
                    for c in r.checks
                    if not c.ok
                ],
            }
            for r in results
        ],
        "portfolios": portfolios or [],
    }
    lines = [
        f"# AERA evaluation report: subset `{data['subset']}`",
        "",
        f"Mode: {data['mode']}. Targets from SRD 8.4.",
        "",
        "| Metric | Result | Target |",
        "|---|---|---|",
        f"| Figure accuracy | {_pct(values['figureAccuracy'])} | 100% |",
        f"| Tier accuracy | {_pct(values['tierAccuracy'])} | 100% |",
        f"| Verifier and tool checks | {_pct(values['checkAccuracy'])} | 100% |",
        f"| Gate decisions | {_pct(values['gateAccuracy'])} | 100% |",
        f"| Multilingual extraction | {_pct(values['extractionAccuracy'])} | 100% |",
        f"| Plan acceptability (scripted plans) | {_pct(values['planAcceptability'])} | >= 90% |",
        f"| Adversarial containment | {_pct(values['adversarialContainment'])} | 100% |",
        f"| Optimiser quality | {_pct(portfolio_score)} | >= 95% of 20 |",
        "| Tool-call accuracy | not measured offline (no model in the loop) | >= 95% |",
        "| Dialogue success | not measured by scripted plans | >= 80% |",
        "| Lab robustness | not measured by this runner | >= 95% of 50 |",
        "| Time to plan, cost per case | not measured offline | p95 <= 90 s; <= USD 0.50 |",
        "",
        f"Cases passed: {_pct(values['casesPassed'])}.",
        "",
        "## Cases by category",
        "",
        "| Category | Passed |",
        "|---|---|",
    ]
    for category in sorted({r.case.category for r in results}):
        group = [r for r in results if r.case.category == category]
        lines.append(f"| {category} | {sum(r.passed for r in group)}/{len(group)} |")
    if subset is None:
        lines += [
            "", "## Method and limits", "",
            "The offline runner uses scripted plan choices and recorded Guardrail, OCR and "
            "language stand-ins. Its 150 cases include parameter and signal-context "
            "variants of the reference seed; 30 adversarial scenarios reuse six hostile "
            "payloads and external-recipient variants. This set was assembled after initial "
            "prompt work, so it does not establish a pre-tuning held-out result. Reported "
            "percentages cover only cases with an assertion for that metric. Live agent, "
            "channel, dialogue and Scenario Lab targets need separate runs.",
        ]
    lines += ["", "## Failures", ""]
    failures = [c for c in data["cases"] if not c["passed"]]
    if not failures:
        lines.append("None.")
    for entry in failures:
        lines.append(f"- **{entry['id']}** ({entry['category']})")
        if entry["error"]:
            lines.append(f"  - error: {entry['error']}")
        for failure in entry["failures"]:
            lines.append(
                f"  - {failure['kind']} `{failure['name']}`: expected {failure['expected']}, "
                f"got {failure['actual']}"
            )
    if portfolios:
        lines += ["", "## Portfolio failures", ""]
        failed_portfolios = [p for p in portfolios if not p["passed"]]
        lines += [
            f"- {p['id']}: objective {p['objectiveCents']} cents versus known optimum "
            f"{p['knownOptimumCents']} cents; selected {p['selectedCases']} "
            f"versus {p['expectedCases']}"
            for p in failed_portfolios
        ] or ["None."]
    return "\n".join(lines) + "\n", data


def run_all(cases: list[EvalCase], mirror_url: str, t0: str = T0) -> list[CaseResult]:
    start = datetime.fromisoformat(t0.replace("Z", "+00:00"))
    return [run_case(case, mirror_url, start) for case in cases]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--subset", default=None, help="e.g. ci; all cases when omitted")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "reports")
    parser.add_argument("--mirror-url", default=None, help="a running Mirror; else one is started")
    args = parser.parse_args(argv)
    os.environ.setdefault("POWERTOOLS_LOG_LEVEL", "WARNING")  # the report, not the audit log
    cases = load_cases(args.subset)
    if args.mirror_url:
        results = run_all(cases, args.mirror_url)
    else:
        from mirror_process import running_mirror

        with running_mirror(T0) as url:
            results = run_all(cases, url)
    portfolios = portfolio_results() if args.subset is None else None
    markdown, data = report(results, args.subset, portfolios)
    args.out.mkdir(parents=True, exist_ok=True)
    name = f"report-{args.subset or 'all'}"
    (args.out / f"{name}.md").write_text(markdown, encoding="utf-8")
    (args.out / f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(markdown)
    return 0 if all(r.passed for r in results) and all(p["passed"] for p in portfolios or []) else 1


_paths()

if __name__ == "__main__":
    sys.exit(main())
