"""SAP client: retry, CSRF, ETag and sourceRef (NFR-REL-01, IR-03, FR-IMP-03)."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from services.shared.sap_client import (
    ApiKeyAuth,
    ClientCredentialsAuth,
    Endpoint,
    SapClient,
    SapNotFoundError,
    SapPreconditionRequiredError,
    SapStaleEtagError,
    SapUnavailableError,
    Target,
    entity_ref,
)

MIRROR = "https://mirror.example"
SANDBOX = "https://sandbox.api.sap.com/s4hanacloud"
PO_SERVICE = "API_PURCHASEORDER_PROCESS_SRV"
PO_URI = f"{MIRROR}/sap/opu/odata/sap/{PO_SERVICE}/A_PurchaseOrder('4500001234')"


class Recorder:
    def __init__(self, responses: list[httpx.Response | Callable[[httpx.Request], httpx.Response]]):
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        return response(request) if callable(response) else response


def envelope(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"d": data})


def po_body() -> dict[str, Any]:
    return {
        "__metadata": {"uri": PO_URI, "type": f"{PO_SERVICE}.A_PurchaseOrder", "etag": 'W/"x"'},
        "PurchaseOrder": "4500001234",
        "Supplier": "1000234",
        "CreationDate": "/Date(1788825600000)/",
    }


def client(recorder: Recorder, sleeps: list[float] | None = None, **kwargs: Any) -> SapClient:
    mirror = Endpoint(base_url=MIRROR, target=Target.MIRROR, auth=None)
    return SapClient(
        read=mirror,
        write=mirror,
        http=httpx.Client(transport=httpx.MockTransport(recorder)),
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
        jitter=lambda: 1.0,
        **kwargs,
    )


# Reads and sourceRef --------------------------------------------------------------------


def test_fr_imp_03_a_read_carries_its_sap_source_reference() -> None:
    recorder = Recorder([envelope(po_body())])

    record = client(recorder).get(PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrder": "4500001234"})

    assert str(recorder.requests[0].url) == (
        f"{MIRROR}/sap/opu/odata/sap/{PO_SERVICE}/A_PurchaseOrder('4500001234')?%24format=json"
    )
    assert record.data["Supplier"] == "1000234"
    assert record.source_ref == f"SAP:{PO_SERVICE}/A_PurchaseOrder('4500001234')"
    assert (
        record.field_ref("Supplier") == f"SAP:{PO_SERVICE}/A_PurchaseOrder('4500001234')/Supplier"
    )
    assert record.target is Target.MIRROR
    assert record.etag == 'W/"x"'


def test_fr_imp_03_query_rows_take_their_reference_from_the_entity_uri() -> None:
    uri = (
        f"{MIRROR}/sap/opu/odata/sap/API_MATERIAL_STOCK_SRV"
        "/A_MatlStkInAcctMod(Material='MAT-48219',Plant='1010')"
    )
    recorder = Recorder(
        [
            envelope(
                {
                    "results": [
                        {"__metadata": {"uri": uri}, "MatlWrhsStkQtyInMatlBaseUnit": "310.000"}
                    ]
                }
            )
        ]
    )

    rows = client(recorder).query(
        "API_MATERIAL_STOCK_SRV", "A_MatlStkInAcctMod", filter="Material eq 'MAT-48219'"
    )

    assert rows[0].source_ref == (
        "SAP:API_MATERIAL_STOCK_SRV/A_MatlStkInAcctMod(Material='MAT-48219',Plant='1010')"
    )
    assert "%24filter=Material+eq+%27MAT-48219%27" in str(recorder.requests[0].url)


def test_ir_03_keys_are_quoted_and_escaped() -> None:
    assert entity_ref("A_X", {"K": "a'b"}) == "A_X('a''b')"
    assert entity_ref("A_X", {"A": "1", "B": "2"}) == "A_X(A='1',B='2')"


def test_ir_01_sandbox_reads_send_the_api_key_header() -> None:
    recorder = Recorder([envelope(po_body())])
    sandbox = Endpoint(
        base_url=SANDBOX, target=Target.SANDBOX, auth=ApiKeyAuth(lambda: "synthetic")
    )
    sap = SapClient(read=sandbox, http=httpx.Client(transport=httpx.MockTransport(recorder)))

    record = sap.get(PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrder": "4500001234"})

    request = recorder.requests[0]
    assert request.headers["APIKey"] == "synthetic"  # pragma: allowlist secret
    assert str(request.url).startswith(f"{SANDBOX}/sap/opu/odata/sap/{PO_SERVICE}/")
    assert record.target is Target.SANDBOX


def test_ir_02_mirror_calls_use_a_cached_client_credentials_token() -> None:
    token_calls: list[httpx.Request] = []

    def token(request: httpx.Request) -> httpx.Response:
        token_calls.append(request)
        return httpx.Response(200, json={"access_token": "t1", "expires_in": 3600})

    recorder = Recorder([envelope(po_body()), envelope(po_body())])
    auth = ClientCredentialsAuth(
        token_url="https://auth.example/oauth/token",
        credentials=lambda: ("client", "synthetic-secret"),  # pragma: allowlist secret
        http=httpx.Client(transport=httpx.MockTransport(token)),
    )
    mirror = Endpoint(base_url=MIRROR, target=Target.MIRROR, auth=auth)
    sap = SapClient(read=mirror, http=httpx.Client(transport=httpx.MockTransport(recorder)))

    sap.get(PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrder": "4500001234"})
    sap.get(PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrder": "4500001234"})

    assert len(token_calls) == 1
    assert "grant_type=client_credentials" in token_calls[0].content.decode()
    assert all(r.headers["Authorization"] == "Bearer t1" for r in recorder.requests)


def test_correlation_headers_are_propagated() -> None:
    recorder = Recorder([envelope(po_body())])

    client(recorder).get(
        PO_SERVICE,
        "A_PurchaseOrder",
        {"PurchaseOrder": "4500001234"},
        case_id="EXC-2026-0914",
        run_id="r1",
    )

    assert recorder.requests[0].headers["x-aera-case-id"] == "EXC-2026-0914"
    assert recorder.requests[0].headers["x-aera-run-id"] == "r1"


# Retry (NFR-REL-01) --------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_nfr_rel_01_reads_retry_on_429_and_5xx_with_exponential_backoff(status: int) -> None:
    sleeps: list[float] = []
    recorder = Recorder([httpx.Response(status), httpx.Response(status), envelope(po_body())])

    record = client(recorder, sleeps).get(
        PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrder": "4500001234"}
    )

    assert record.data["PurchaseOrder"] == "4500001234"
    assert len(recorder.requests) == 3
    assert sleeps == [0.5, 1.0]


def test_nfr_rel_01_three_failed_attempts_raise_unavailable() -> None:
    recorder = Recorder([httpx.Response(503)] * 3)

    with pytest.raises(SapUnavailableError, match="3 attempts"):
        client(recorder).get(PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrder": "4500001234"})
    assert len(recorder.requests) == 3


def test_nfr_rel_01_backoff_has_jitter() -> None:
    sleeps: list[float] = []
    recorder = Recorder([httpx.Response(503), envelope(po_body())])
    mirror = Endpoint(base_url=MIRROR, target=Target.MIRROR, auth=None)
    SapClient(
        read=mirror,
        http=httpx.Client(transport=httpx.MockTransport(recorder)),
        sleep=sleeps.append,
        jitter=lambda: 1.3,
    ).get(PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrder": "4500001234"})

    assert sleeps == [pytest.approx(0.65)]


def test_nfr_rel_01_network_errors_are_retried() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    recorder = Recorder([fail, envelope(po_body())])

    assert client(recorder).get(PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrder": "4500001234"})


def test_a_missing_entity_is_not_retried() -> None:
    recorder = Recorder([httpx.Response(404)])

    with pytest.raises(SapNotFoundError):
        client(recorder).get(PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrder": "1"})
    assert len(recorder.requests) == 1


# Writes: CSRF and ETag (IR-02) ---------------------------------------------------------


def csrf_response() -> httpx.Response:
    return httpx.Response(
        200, headers={"x-csrf-token": "tok", "set-cookie": "SAP_SESSIONID_MIRROR=s1; Path=/"}
    )


def test_ir_02_a_create_fetches_a_csrf_token_and_sends_it_with_the_session() -> None:
    created = {**po_body(), "PurchaseOrder": "4500001235"}
    recorder = Recorder([csrf_response(), httpx.Response(201, json={"d": created})])

    record = client(recorder).create(PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrderType": "UB"})

    fetch, post = recorder.requests
    assert fetch.method == "GET" and fetch.headers["x-csrf-token"] == "Fetch"
    assert post.method == "POST"
    assert post.headers["x-csrf-token"] == "tok"
    assert "SAP_SESSIONID_MIRROR=s1" in post.headers["cookie"]
    assert json.loads(post.content) == {"PurchaseOrderType": "UB"}
    assert record.data["PurchaseOrder"] == "4500001235"


def test_ir_02_an_expired_csrf_token_is_refetched_once() -> None:
    required = httpx.Response(403, headers={"x-csrf-token": "Required"})
    recorder = Recorder(
        [csrf_response(), required, csrf_response(), httpx.Response(201, json={"d": po_body()})]
    )

    client(recorder).create(PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrderType": "UB"})

    assert [r.method for r in recorder.requests] == ["GET", "POST", "GET", "POST"]


def test_nfr_rel_01_a_create_is_not_retried_after_an_ambiguous_500() -> None:
    recorder = Recorder([csrf_response(), httpx.Response(500)])

    with pytest.raises(SapUnavailableError):
        client(recorder).create(PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrderType": "UB"})
    assert [r.method for r in recorder.requests] == ["GET", "POST"]


def test_nfr_rel_01_a_create_is_retried_when_the_server_refused_it_unprocessed() -> None:
    recorder = Recorder(
        [csrf_response(), httpx.Response(503), httpx.Response(201, json={"d": po_body()})]
    )

    client(recorder).create(PO_SERVICE, "A_PurchaseOrder", {"PurchaseOrderType": "UB"})
    assert [r.method for r in recorder.requests] == ["GET", "POST", "POST"]


def test_ir_02_an_update_sends_if_match_and_reports_a_stale_etag() -> None:
    recorder = Recorder([csrf_response(), httpx.Response(204), httpx.Response(412)])
    sap = client(recorder)
    keys = {"PurchasingDocument": "4500001234", "PurchasingDocumentItem": "10", "ScheduleLine": "1"}

    sap.update(
        PO_SERVICE,
        "A_PurchaseOrderScheduleLine",
        keys,
        {"ScheduleLineDeliveryDate": "x"},
        etag='W/"e1"',
    )
    with pytest.raises(SapStaleEtagError):
        sap.update(
            PO_SERVICE,
            "A_PurchaseOrderScheduleLine",
            keys,
            {"ScheduleLineDeliveryDate": "x"},
            etag='W/"e1"',
        )

    patch = recorder.requests[1]
    assert patch.method == "PATCH" and patch.headers["if-match"] == 'W/"e1"'


def test_ir_02_a_missing_precondition_is_reported() -> None:
    recorder = Recorder([csrf_response(), httpx.Response(428)])

    with pytest.raises(SapPreconditionRequiredError):
        client(recorder).update(
            PO_SERVICE, "A_PurchaseOrderScheduleLine", {"A": "1"}, {}, etag='W/"e"'
        )


def test_ir_03_writes_go_to_the_write_base_only() -> None:
    recorder = Recorder([envelope(po_body())])
    read_only = SapClient(
        read=Endpoint(base_url=SANDBOX, target=Target.SANDBOX, auth=None),
        http=httpx.Client(transport=httpx.MockTransport(recorder)),
    )

    with pytest.raises(PermissionError, match="no write endpoint"):
        read_only.create(PO_SERVICE, "A_PurchaseOrder", {})
    assert recorder.requests == []
