from datetime import timedelta
from typing import Any

import pytest

from services.routing.logic import route
from services.routing.store import ApprovalConflict, ControlStore
from services.shared.dynamo import to_item
from services.verifier.tests.test_verification import NOW, limits, verified

CASE = "EXC-2026-0914"


def setup_store(dynamodb: Any) -> tuple[ControlStore, str, str]:
    store = ControlStore(dynamodb, "test")
    dynamodb.put_item(
        TableName=store.table, Item=to_item({"PK": f"CASE#{CASE}", "SK": "META", "planVersion": 1})
    )
    for limit in limits():
        dynamodb.put_item(
            TableName=store.config,
            Item=to_item({"PK": f"APPR#{limit.user_id}", **limit.model_dump(by_alias=True)}),
        )
    dynamodb.put_item(
        TableName=store.config, Item=to_item({"PK": "CFG#KILL_SWITCH", "value": "off"})
    )
    result = verified()
    routing = route(
        result, now=NOW, stockout=NOW + timedelta(hours=24), plant="1010", limits=limits()
    )
    store.save(result, routing, plant="1010", now=NOW)
    return store, routing.version_hash, routing.parts[0].id


def decide(store: ControlStore, digest: str, **kwargs: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = dict(
        actor="primary",
        groups=frozenset({"approver"}),
        version_hash=digest,
        decision="APPROVED",
        comment="Checked",
        now=NOW,
    )
    arguments.update(kwargs)
    return store.decide(CASE, **arguments)


def test_FR_RTE_03_atomic_decision_and_payload_bound_replay(dynamodb: Any) -> None:
    store, digest, part = setup_store(dynamodb)
    first = decide(store, digest)
    assert first["decision"] == "APPROVED"
    assert decide(store, digest)["decidedAt"] == first["decidedAt"]
    assert store.get(CASE, f"OUTBOX#PlanApproved#{part}") is not None
    with pytest.raises(ApprovalConflict):
        decide(store, digest, decision="REJECTED")


def test_FR_RTE_03_stale_hash_and_version(dynamodb: Any) -> None:
    store, digest, _ = setup_store(dynamodb)
    with pytest.raises(ApprovalConflict):
        decide(store, "stale")
    dynamodb.update_item(
        TableName=store.table,
        Key=to_item({"PK": f"CASE#{CASE}", "SK": "META"}),
        UpdateExpression="SET planVersion = :v",
        ExpressionAttributeValues=to_item({":v": 2}),
    )
    with pytest.raises(ApprovalConflict):
        decide(store, digest)


def test_BR_05_BR_16_role_assignment_limit_and_kill_switch(dynamodb: Any) -> None:
    store, digest, _ = setup_store(dynamodb)
    mutations: list[dict[str, Any]] = [{"groups": frozenset({"planner"})}, {"actor": "backup"}]
    for changes in mutations:
        with pytest.raises(PermissionError):
            decide(store, digest, **changes)
    dynamodb.update_item(
        TableName=store.config,
        Key={"PK": {"S": "APPR#primary"}},
        UpdateExpression="SET limitUsd = :v",
        ExpressionAttributeValues=to_item({":v": 1}),
    )
    with pytest.raises(PermissionError):
        decide(store, digest)
    dynamodb.update_item(
        TableName=store.config,
        Key={"PK": {"S": "APPR#primary"}},
        UpdateExpression="SET limitUsd = :v",
        ExpressionAttributeValues=to_item({":v": 50000}),
    )
    dynamodb.put_item(
        TableName=store.config, Item=to_item({"PK": "CFG#KILL_SWITCH", "value": "on"})
    )
    with pytest.raises(ApprovalConflict, match="kill switch"):
        decide(store, digest)


def test_BR_23_reminder_backup_and_expired_approval(dynamodb: Any) -> None:
    store, digest, part = setup_store(dynamodb)
    store.tick(CASE, part, now=NOW + timedelta(hours=2))
    reminded = store.get(CASE, f"PART#{part}")
    assert reminded is not None and reminded["reminded"]
    store.tick(CASE, part, now=NOW + timedelta(hours=4))
    pending = store.get(CASE, f"PART#{part}")
    assert pending is not None
    assert pending["expired"] and pending["approver"] == "backup"
    with pytest.raises(ApprovalConflict, match="expired"):
        decide(store, digest, actor="backup", now=NOW + timedelta(hours=4))
    with pytest.raises(PermissionError):
        decide(store, digest, now=NOW + timedelta(hours=4))
