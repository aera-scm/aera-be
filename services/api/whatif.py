"""Projection and what-if for the console (FR-SIM-01..03, FR-UI-13, AT-19).

`GET /cases/{id}/projection?option=` projects the plan of record: the baseline and either
one option or the chosen options together. `POST /cases/{id}/whatif` recalculates one
option with edited parameters, projects it and runs the Verifier's checks on it; nothing is
recorded, so the plan of record, its route and its drafts stay as they are.
"""

from __future__ import annotations

from typing import Any

from services.optimizer.inputs import CaseProjector, projection_json
from services.shared.dynamo import from_item, table_name
from services.shared.models import Case, Option, PlanRecord, ProposedPlan
from services.tools.calc import draft_key
from services.tools.context import ToolContext, ToolError, json_dict
from services.tools.simulate import ACTION_TYPE, build_option
from services.verifier.evidence import EvidenceReader, Source
from services.verifier.logic import Grounding, verify


class WhatIfError(ValueError):
    pass


def _record(ctx: ToolContext, case: Case) -> PlanRecord | None:
    if case.plan_version < 1:
        return None
    item = ctx.dynamodb.get_item(
        TableName=table_name("cases", ctx.env),
        Key={"PK": {"S": f"CASE#{case.case_id}"}, "SK": {"S": f"PLAN#{case.plan_version}"}},
        ConsistentRead=True,
    ).get("Item")
    if item is None:
        return None
    data = from_item(item)
    for key in ("PK", "SK"):
        data.pop(key, None)
    return PlanRecord.model_validate(data)


def _option(record: PlanRecord | None, option_id: str) -> Option:
    found = next((o for o in record.plan.options if o.id == option_id), None) if record else None
    if found is None:
        raise WhatIfError(f"option {option_id} is not in the plan of record")
    return found


def projection(ctx: ToolContext, case: Case, option_id: str | None) -> dict[str, Any]:
    record = _record(ctx, case)
    projector = CaseProjector(ctx, case)
    if option_id:
        chosen = [_option(record, option_id)]
    elif record is not None:
        chosen = [o for o in record.plan.options if o.id in record.plan.chosen]
    else:
        chosen = []
    return json_dict(
        {
            "caseId": case.case_id,
            "planVersion": case.plan_version,
            "option": option_id or ("plan" if chosen else None),
            "baseline": projection_json(projector.projections([])),
            "projection": projection_json(projector.projections(chosen)),
        }
    )


def whatif(ctx: ToolContext, case: Case, option_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    record = _record(ctx, case)
    original = _option(record, option_id)
    draft = ctx.dynamodb.get_item(
        TableName=table_name("cases", ctx.env),
        Key={
            "PK": {"S": f"CASE#{case.case_id}"},
            "SK": {
                "S": "DRAFT#"
                + draft_key(original.cost_usd, original.coverage_units, original.cost_source_ref)
            },
        },
        ConsistentRead=True,
    ).get("Item")
    stored = from_item(draft) if draft else {}
    if not isinstance(stored.get("params"), dict):
        raise WhatIfError(f"option {option_id} has no recalculation parameters")
    base = dict(stored["params"])
    action_type = ACTION_TYPE.get(original.actions[0].type)
    if action_type is None:
        raise WhatIfError(f"option {option_id} cannot be recalculated")
    params = {**base, **changes}
    try:
        edited = build_option(ctx, case.case_id, option_id, action_type, params)
    except ToolError as error:
        raise WhatIfError(str(error)) from None
    plan = ProposedPlan(
        case_id=case.case_id,
        plan_version=max(case.plan_version, 1),
        options=[edited],
        chosen=[option_id],
        total_cost_usd=edited.cost_usd,
        coverage_units=edited.coverage_units,
        rationale="what-if",
    )
    now = ctx.now()
    facts = EvidenceReader(ctx).gather(
        case, plan, sources={option_id: Source(action_type, params, now)}
    )
    checked = verify(
        plan,
        facts.evidence,
        now=now,
        grounding=Grounding(),  # the grounding call is made for real plans only
        corroboration=facts.corroboration,
        proposed_at=now,
        minimum_cover=ctx.config.decimal("DONOR_MIN_COVER_DAYS"),
    )
    projector = CaseProjector(ctx, case)
    return json_dict(
        {
            "caseId": case.case_id,
            "optionId": option_id,
            "params": params,
            "option": edited.model_dump(mode="json", by_alias=True),
            "checks": [
                c.model_dump(mode="json", by_alias=True)
                for c in checked.record.checks
                if c.option_id == option_id and c.check_id != "V-13"
            ],
            "baseline": projection_json(projector.projections([])),
            "projection": projection_json(projector.projections([edited])),
            "planOfRecord": {"version": case.plan_version, "unchanged": True},
            "computedAt": now,
        }
    )
