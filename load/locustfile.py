"""AT-28: 300 synthetic manual signals/hour with 30 active Locust users for 30 min.

Requires 30 distinct active cases already seeded in the deployed environment. A run
refuses to start without them. The final ledger checks every accepted signal ID against
all three gate states; Locust's CSV records response latency and failures separately.
"""

from __future__ import annotations

import os
import random
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from gevent import Greenlet, sleep, spawn  # type: ignore[import-untyped]
from locust import HttpUser, constant_pacing, events, task
from locust.env import Environment

from load.ledger import LoadLedger

INTERVAL_SECONDS = 360
MATERIALS = (
    ("4500001234", "MAT-48219", "orders@krieger-guss.example"),
    ("4500001240", "MAT-51002", "orders@krieger-guss.example"),
    ("4500001251", "MAT-33871", "sales@halim-presisi.example"),
    ("4500001262", "MAT-20114", "orders@krieger-guss.example"),
    ("4500001273", "MAT-60417", "sales@halim-presisi.example"),
    ("4500001284", "MAT-72055", "orders@krieger-guss.example"),
)
LEDGER = LoadLedger()
RUN_ID = uuid.uuid4().hex[:12]
ACTIVE_START = 0
MIN_ACTIVE = 0
MONITOR: Greenlet | None = None
MONITOR_ERROR: str | None = None
STARTED_AT = 0.0


def _token() -> str:
    token = os.environ.get("AERA_LOAD_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("AERA_LOAD_ACCESS_TOKEN is required")
    return token


def _rows(host: str, path: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{host.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError(f"{path} did not return a list")
    return rows


def _active_count(host: str) -> int:
    return len({str(row["caseId"]) for row in _rows(host, "/cases")})


def _monitor(host: str) -> None:
    global MIN_ACTIVE, MONITOR_ERROR
    while True:
        sleep(30)
        try:
            MIN_ACTIVE = min(MIN_ACTIVE, _active_count(host))
        except Exception as error:
            MONITOR_ERROR = str(error)
            return


@events.test_start.add_listener  # type: ignore[untyped-decorator]
def ready(environment: Environment, **_: Any) -> None:
    global ACTIVE_START, MIN_ACTIVE, MONITOR, STARTED_AT
    host = environment.host
    if not host or not host.startswith("https://") or host.endswith("/"):
        raise RuntimeError("--host must be the HTTPS API stage URL without a trailing slash")
    ACTIVE_START = _active_count(host)
    MIN_ACTIVE = ACTIVE_START
    if ACTIVE_START < 30:
        raise RuntimeError(f"AT-28 needs 30 distinct active cases; found {ACTIVE_START}")
    STARTED_AT = time.monotonic()
    MONITOR = spawn(_monitor, host)


@events.test_stop.add_listener  # type: ignore[untyped-decorator]
def finished(environment: Environment, **_: Any) -> None:
    if not environment.host:
        return
    import json

    if MONITOR is not None:
        MONITOR.kill(block=True)
    rows = [
        row
        for status in ("RECEIVED", "ACCEPTED", "QUARANTINED")
        for row in _rows(environment.host, f"/signals?status={status}")
    ]
    report = LEDGER.report(rows, MIN_ACTIVE)
    report["runId"] = RUN_ID
    report["durationSeconds"] = round(time.monotonic() - STARTED_AT, 1)
    report["signalsPerHour"] = round(
        3600 * report["submitted"] / max(1, report["durationSeconds"]), 1
    )
    report["activeCaseMonitorError"] = MONITOR_ERROR
    report["requestFailures"] = environment.stats.total.num_failures
    report["requestP95Ms"] = environment.stats.total.get_response_time_percentile(0.95)
    report["pass"] = bool(
        report["pass"]
        and report["durationSeconds"] >= 1795
        and report["signalsPerHour"] >= 300
        and report["requestFailures"] == 0
        and MONITOR_ERROR is None
    )
    Path(os.environ.get("AERA_LOAD_RESULT", "load-result.json")).write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    if not report["pass"]:
        environment.process_exit_code = 1


class SignalUser(HttpUser):
    wait_time = constant_pacing(INTERVAL_SECONDS)  # type: ignore[no-untyped-call]

    def on_start(self) -> None:
        import gevent

        gevent.sleep(random.uniform(0, INTERVAL_SECONDS))

    @task
    def submit(self) -> None:
        po, material, sender = random.choice(MATERIALS)
        key = f"load-{RUN_ID}-{uuid.uuid4().hex}"
        with self.client.post(
            "/signals",
            json={
                "sender": sender,
                "text": f"Synthetic load signal {key}: PO {po} {material} late",
            },
            headers={
                "Authorization": f"Bearer {_token()}",
                "Idempotency-Key": key,
            },
            name="POST /signals",
            catch_response=True,
        ) as response:
            if response.status_code != 202:
                response.failure(f"signal refused: HTTP {response.status_code}")
                return
            try:
                LEDGER.record(response.json())
            except ValueError as error:
                response.failure(str(error))
