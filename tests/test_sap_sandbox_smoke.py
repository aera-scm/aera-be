"""IR-01, NFR-SEC-03/05: synthetic transport, never live evidence."""

import json
import ssl
from pathlib import Path
from typing import Any

import pytest
import sap_sandbox_smoke as smoke
from sap_transport import SapError, get

FIXTURE = Path(__file__).parent / "fixtures/sap/purchase_orders.json"


def test_ir_01_read_only_path_and_environment_key(capsys: Any) -> None:
    calls: list[tuple[str, str, str]] = []

    def fetch(path: str, key: str, accept: str) -> bytes:
        calls.append((path, key, accept))
        return FIXTURE.read_bytes()

    environment = {"SAP_SANDBOX_API_KEY": "synthetic-test-value"}  # pragma: allowlist secret
    assert smoke.main(environ=environment, fetch=fetch) == 0
    assert calls == [(smoke.PATH, "synthetic-test-value", "application/json")]
    output = capsys.readouterr().out
    assert json.loads(output)["count"] == 1
    assert "sourceRef" in json.loads(output)
    assert "SYNTHETIC-001" not in output
    assert "synthetic-test-value" not in output


@pytest.mark.parametrize(
    "body",
    [
        b"bad",
        b"[]",
        b"{}",
        b'{"d":[]}',
        b'{"d":{"results":{}}}',
        b'{"d":{"results":[1]}}',
        b'{"d":{"results":[]}}',
    ],
)
def test_ir_01_malformed_or_empty_envelope(body: bytes) -> None:
    with pytest.raises(SapError):
        smoke.purchase_order_count(body)


def test_missing_key_makes_no_request(capsys: Any) -> None:
    def fetch(*args: str) -> bytes:
        pytest.fail("missing credential must prevent network access")

    assert smoke.main(environ={}, fetch=fetch) == 1
    assert "set SAP_SANDBOX_API_KEY in the process environment" in capsys.readouterr().err


@pytest.mark.parametrize("status", [301, 302, 400, 401, 403, 429, 500])
def test_ir_01_http_failures_are_sanitized(status: int) -> None:
    class Response:
        def __init__(self) -> None:
            self.status = status

        def read(self, size: int) -> bytes:
            pytest.fail("error body must not be read")

    class Connection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def request(self, *args: Any, **kwargs: Any) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    with pytest.raises(SapError, match=f"HTTP {status}"):
        get(smoke.PATH, "synthetic-test-value", "application/json", connection=Connection)


def test_nfr_sec_05_tls_timeout_header_and_no_redirects() -> None:
    calls: list[Any] = []

    class Response:
        status = 200

        def read(self, size: int) -> bytes:
            return b"{}"

    class Connection:
        def __init__(self, host: str, *, timeout: int, context: ssl.SSLContext) -> None:
            assert host == "sandbox.api.sap.com"
            assert 0 < timeout <= 30
            assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname

        def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
            calls.append((method, path, headers))

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            calls.append("closed")

    assert (
        get(smoke.PATH, "synthetic-test-value", "application/json", connection=Connection) == b"{}"
    )
    assert calls[0] == (
        "GET",
        smoke.PATH,
        {
            "APIKey": "synthetic-test-value",  # pragma: allowlist secret
            "Accept": "application/json",
        },
    )
    assert calls[-1] == "closed"


@pytest.mark.parametrize("error", [TimeoutError("sensitive"), OSError("sensitive")])
def test_transport_exception_details_are_not_reported(error: Exception) -> None:
    def connection(*args: Any, **kwargs: Any) -> Any:
        raise error

    with pytest.raises(SapError) as caught:
        get(smoke.PATH, "synthetic-test-value", "application/json", connection=connection)
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize(
    "path", ["https://other.example/x", "//other.example", "/other", smoke.PATH + "\r\nX: y"]
)
def test_transport_rejects_unapproved_paths(path: str) -> None:
    with pytest.raises(SapError):
        get(path, "synthetic-test-value", "application/json")
