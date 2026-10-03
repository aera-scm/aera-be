"""One SAP client for the sandbox, the Mirror and a licensed tenant (IR-01..IR-03).

Reads and writes go to configurable base URLs (`SAP_READ_BASE`, `SAP_WRITE_BASE`) under
the S/4HANA Gateway path `/sap/opu/odata/sap/<service>`, so switching systems is
configuration only. Every record carries its `sourceRef` (FR-IMP-03) and call target
(sandbox or mirror, SRD 6.6.1). Writes fetch a CSRF token and send `If-Match` exactly as
S/4HANA expects. Calls retry on 429/5xx with exponential backoff and jitter (NFR-REL-01);
a create is retried only when the server said it did not process it (429, 503), so an
ambiguous failure never risks a duplicate document.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

GATEWAY = "/sap/opu/odata/sap"
_IDEMPOTENT_RETRY = frozenset({429, 500, 502, 503, 504})
_CREATE_RETRY = frozenset({429, 503})
_SAFE_NETWORK_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)


class SapError(Exception):
    """Base class; messages never contain credentials or response bodies."""


class SapNotFoundError(SapError):
    pass


class SapStaleEtagError(SapError):
    pass


class SapPreconditionRequiredError(SapError):
    pass


class SapUnavailableError(SapError):
    pass


class SapRequestError(SapError):
    def __init__(self, status: int, operation: str) -> None:
        self.status = status
        super().__init__(f"SAP {operation} failed with HTTP {status}")


class _CsrfRequiredError(SapError):
    pass


class Target(StrEnum):
    SANDBOX = "sandbox"
    MIRROR = "mirror"
    TENANT = "tenant"


class Auth(Protocol):
    def apply(self, headers: dict[str, str]) -> None: ...


class ApiKeyAuth:
    """SAP Business Accelerator Hub sandbox (IR-01): header `APIKey`."""

    def __init__(self, key: Callable[[], str]) -> None:
        self._key = key

    def apply(self, headers: dict[str, str]) -> None:
        headers["APIKey"] = self._key()


class ClientCredentialsAuth:
    """OAuth 2.0 client credentials (the Mirror's XSUAA), token cached until near expiry."""

    def __init__(
        self,
        *,
        token_url: str,
        credentials: Callable[[], tuple[str, str]],
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token_url = token_url
        self._credentials = credentials
        self._http = http or httpx.Client(timeout=10.0)
        self._clock = clock
        self._token: str | None = None
        self._expires = 0.0

    def apply(self, headers: dict[str, str]) -> None:
        if self._token is None or self._clock() >= self._expires:
            client_id, secret = self._credentials()
            response = self._http.post(
                self._token_url,
                data={"grant_type": "client_credentials"},
                auth=(client_id, secret),
            )
            if response.status_code != 200:
                raise SapError(f"token request failed with HTTP {response.status_code}")
            body = response.json()
            self._token = str(body["access_token"])
            self._expires = self._clock() + max(0.0, float(body.get("expires_in", 300)) - 60)
        headers["Authorization"] = f"Bearer {self._token}"


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    target: Target
    auth: Auth | None


def _key_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def entity_ref(entity_set: str, keys: Mapping[str, str]) -> str:
    """`A_X('v')` for a single key, `A_X(A='1',B='2')` otherwise (OData V2)."""
    if len(keys) == 1:
        return f"{entity_set}({_key_literal(next(iter(keys.values())))})"
    inner = ",".join(f"{name}={_key_literal(value)}" for name, value in keys.items())
    return f"{entity_set}({inner})"


@dataclass(frozen=True)
class SapRecord:
    data: dict[str, Any]
    source_ref: str
    target: Target
    etag: str | None = None

    def field_ref(self, name: str) -> str:
        return f"{self.source_ref}/{name}"


@dataclass
class SapClient:
    read: Endpoint
    write: Endpoint | None = None
    http: httpx.Client | None = None
    sleep: Callable[[float], None] = time.sleep
    jitter: Callable[[], float] = field(default=lambda: random.uniform(0.5, 1.5))
    timeout: float = 10.0
    attempts: int = 3
    base_delay: float = 0.5
    _csrf: dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.http is None:
            self.http = httpx.Client(timeout=self.timeout)

    # Reads ------------------------------------------------------------------------------

    def get(
        self,
        service: str,
        entity_set: str,
        keys: Mapping[str, str],
        *,
        expand: str | None = None,
        select: str | None = None,
        case_id: str | None = None,
        run_id: str | None = None,
    ) -> SapRecord:
        path = entity_ref(entity_set, keys)
        params = self._params(expand=expand, select=select)
        response = self._send(
            "GET", self.read, service, path, params=params, case_id=case_id, run_id=run_id
        )
        return self._record(service, response.json()["d"], self.read.target, fallback=path)

    def query(
        self,
        service: str,
        entity_set: str,
        *,
        filter: str | None = None,
        top: int | None = None,
        select: str | None = None,
        expand: str | None = None,
        orderby: str | None = None,
        case_id: str | None = None,
        run_id: str | None = None,
        max_pages: int = 20,
    ) -> list[SapRecord]:
        params = self._params(filter=filter, top=top, select=select, expand=expand, orderby=orderby)
        records: list[SapRecord] = []
        path: str | None = entity_set
        for _ in range(max_pages):
            response = self._send(
                "GET",
                self.read,
                service,
                path or entity_set,
                params=params,
                case_id=case_id,
                run_id=run_id,
            )
            body = response.json()["d"]
            rows = body["results"] if isinstance(body, dict) and "results" in body else body
            records.extend(self._record(service, row, self.read.target) for row in rows)
            next_link = body.get("__next") if isinstance(body, dict) else None
            if not next_link:
                break
            parts = urlsplit(next_link)
            path = parts.path.split(f"{GATEWAY}/{service}/", 1)[-1]
            params = dict(httpx.QueryParams(parts.query))
        return records

    # Writes -----------------------------------------------------------------------------

    def create(
        self,
        service: str,
        entity_set: str,
        payload: Mapping[str, Any],
        *,
        case_id: str | None = None,
        run_id: str | None = None,
    ) -> SapRecord:
        endpoint = self._writer()
        response = self._write(
            "POST", endpoint, service, entity_set, dict(payload), None, case_id, run_id
        )
        return self._record(service, response.json()["d"], endpoint.target)

    def create_under(
        self,
        service: str,
        entity_set: str,
        keys: Mapping[str, str],
        navigation: str,
        payload: Mapping[str, Any],
        *,
        case_id: str | None = None,
        run_id: str | None = None,
    ) -> SapRecord:
        endpoint = self._writer()
        path = f"{entity_ref(entity_set, keys)}/{navigation}"
        response = self._write(
            "POST", endpoint, service, path, dict(payload), None, case_id, run_id
        )
        return self._record(service, response.json()["d"], endpoint.target)

    def update(
        self,
        service: str,
        entity_set: str,
        keys: Mapping[str, str],
        changes: Mapping[str, Any],
        *,
        etag: str,
        case_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        endpoint = self._writer()
        path = entity_ref(entity_set, keys)
        self._write("PATCH", endpoint, service, path, dict(changes), etag, case_id, run_id)

    # Internals --------------------------------------------------------------------------

    def delete(
        self,
        service: str,
        entity_set: str,
        keys: Mapping[str, str],
        *,
        etag: str,
        case_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        self._write(
            "DELETE",
            self._writer(),
            service,
            entity_ref(entity_set, keys),
            {},
            etag,
            case_id,
            run_id,
        )

    def _writer(self) -> Endpoint:
        if self.write is None:
            raise PermissionError("no write endpoint configured (SAP_WRITE_BASE)")
        return self.write

    @staticmethod
    def _params(**options: Any) -> dict[str, str]:
        params = {"$format": "json"}
        for name, value in options.items():
            if value is not None:
                params[f"${name}"] = str(value)
        return params

    def _url(self, endpoint: Endpoint, service: str, path: str) -> str:
        encoded = quote(path, safe="()=',/$")
        return f"{endpoint.base_url.rstrip('/')}{GATEWAY}/{service}/{encoded}"

    def _headers(
        self, endpoint: Endpoint, case_id: str | None, run_id: str | None
    ) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if endpoint.auth is not None:
            endpoint.auth.apply(headers)
        if case_id:
            headers["x-aera-case-id"] = case_id
        if run_id:
            headers["x-aera-run-id"] = run_id
        return headers

    def _record(
        self, service: str, row: dict[str, Any], target: Target, *, fallback: str | None = None
    ) -> SapRecord:
        metadata = row.get("__metadata", {})
        uri = metadata.get("uri", "")
        tail = (
            uri.split(f"{GATEWAY}/{service}/", 1)[1] if f"{GATEWAY}/{service}/" in uri else fallback
        )
        data = {key: value for key, value in row.items() if key != "__metadata"}
        return SapRecord(
            data=data,
            source_ref=f"SAP:{service}/{tail}",
            target=target,
            etag=metadata.get("etag"),
        )

    def _write(
        self,
        method: str,
        endpoint: Endpoint,
        service: str,
        path: str,
        body: dict[str, Any],
        etag: str | None,
        case_id: str | None,
        run_id: str | None,
    ) -> httpx.Response:
        for refetched in (False, True):
            token = self._csrf.get(service) or self._fetch_csrf(endpoint, service)
            extra = {"x-csrf-token": token}
            if etag is not None:
                extra["If-Match"] = etag
            try:
                return self._send(
                    method,
                    endpoint,
                    service,
                    path,
                    json=body,
                    extra=extra,
                    case_id=case_id,
                    run_id=run_id,
                )
            except _CsrfRequiredError:
                self._csrf.pop(service, None)
                if refetched:
                    raise SapRequestError(403, f"{method} {service}") from None
        raise AssertionError("unreachable")

    def _fetch_csrf(self, endpoint: Endpoint, service: str) -> str:
        response = self._send(
            "GET", endpoint, service, "", extra={"x-csrf-token": "Fetch"}, params={}
        )
        token = response.headers.get("x-csrf-token")
        if not token:
            raise SapError(f"{service} returned no CSRF token")
        self._csrf[service] = str(token)
        return str(token)

    def _send(
        self,
        method: str,
        endpoint: Endpoint,
        service: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: dict[str, Any] | None = None,
        extra: Mapping[str, str] | None = None,
        case_id: str | None = None,
        run_id: str | None = None,
    ) -> httpx.Response:
        assert self.http is not None
        retry_on = _CREATE_RETRY if method == "POST" else _IDEMPOTENT_RETRY
        operation = f"{method} {service}"
        for attempt in range(1, self.attempts + 1):
            headers = self._headers(endpoint, case_id, run_id)
            headers.update(extra or {})
            try:
                response = self.http.request(
                    method,
                    self._url(endpoint, service, path),
                    params=dict(params) if params is not None else None,
                    json=json,
                    headers=headers,
                    timeout=self.timeout,
                )
            except httpx.TransportError as error:
                retryable = isinstance(error, _SAFE_NETWORK_ERRORS) or method != "POST"
                if not retryable or attempt == self.attempts:
                    raise SapUnavailableError(
                        f"{operation} failed: {type(error).__name__}"
                    ) from None
                self.sleep(self.base_delay * 2 ** (attempt - 1) * self.jitter())
                continue
            status = response.status_code
            if status < 400:
                return response
            if status == 403 and response.headers.get("x-csrf-token", "").lower() == "required":
                raise _CsrfRequiredError(operation)
            if status == 404:
                raise SapNotFoundError(f"{operation}: {path} not found")
            if status == 412:
                raise SapStaleEtagError(f"{operation}: ETag is stale")
            if status == 428:
                raise SapPreconditionRequiredError(f"{operation}: If-Match required")
            if status in retry_on:
                if attempt == self.attempts:
                    raise SapUnavailableError(
                        f"{operation} failed with HTTP {status} after {self.attempts} attempts"
                    )
                self.sleep(self.base_delay * 2 ** (attempt - 1) * self.jitter())
                continue
            if status >= 500:
                raise SapUnavailableError(f"{operation} failed with HTTP {status}; not retried")
            raise SapRequestError(status, operation)
        raise AssertionError("unreachable")
