"""Verify the running Compose stack through its public contracts."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes


BASE_URL = os.getenv("CI_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
GATEWAY_KEY = os.getenv("CI_GATEWAY_KEY", "")
OPERATIONS_KEY = os.getenv("CI_OPERATIONS_KEY", "")


def main() -> None:
    if not GATEWAY_KEY or not OPERATIONS_KEY:
        raise SystemExit("CI_GATEWAY_KEY and CI_OPERATIONS_KEY are required")

    _wait_until_ready()
    _assert_liveness()
    _assert_v2_identity_and_history()
    _assert_v1_routes_removed()
    _assert_operations_authentication()
    _assert_agent_inventory()
    _assert_metrics()
    print(
        "Live stack smoke passed: readiness, v2 identity/history, retired v1, "
        "operations auth, and metrics"
    )


def _wait_until_ready() -> None:
    deadline = time.monotonic() + 240
    last_error = "unknown readiness failure"
    while time.monotonic() < deadline:
        try:
            result = _request("GET", "/readyz", timeout=5)
            payload = _json(result)
            checks = payload.get("checks", {})
            if (
                result.status == 200
                and payload.get("status") == "ready"
                and _readiness_checks_pass(checks)
            ):
                return
            last_error = f"status={result.status} body={payload}"
        except (HTTPError, OSError, TimeoutError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(5)
    raise SystemExit(f"stack did not become ready: {last_error}")


def _readiness_checks_pass(checks: object) -> bool:
    if not isinstance(checks, dict) or not checks:
        return False
    return all(
        outcome == "ok" or (dependency == "knowledge" and outcome == "disabled")
        for dependency, outcome in checks.items()
    )


def _assert_liveness() -> None:
    result = _request("GET", "/livez")
    payload = _json(result)
    _require(result.status == 200 and payload.get("status") == "ok", "liveness")


def _assert_v2_identity_and_history() -> None:
    identity = _request("GET", "/api/v2/me", headers={"X-API-Key": GATEWAY_KEY})
    identity_payload = _json(identity)
    _require(
        identity.status == 200
        and isinstance(identity_payload.get("principal_id"), str)
        and bool(identity_payload.get("allowed_modes")),
        "v2 identity",
    )

    history = _request(
        "GET",
        "/api/v2/conversations?mode=shopper",
        headers={"X-API-Key": GATEWAY_KEY},
    )
    if history.status == 200:
        _require(isinstance(_json(history).get("conversations"), list), "v2 history")
        return

    payload = _json(history)
    _require(
        history.status == 503
        and payload.get("error", {}).get("code") == "v2.runtime_unavailable"
        and GATEWAY_KEY not in history.body.decode("utf-8"),
        "v2 history availability",
    )


def _assert_v1_routes_removed() -> None:
    result = _request(
        "POST",
        "/api/v1/chat",
        headers={"X-API-Key": GATEWAY_KEY, "Content-Type": "application/json"},
        body=json.dumps({"message": "Tìm sách"}).encode("utf-8"),
    )
    _require(result.status == 404, "v1 route removal")


def _assert_operations_authentication() -> None:
    result = _request("GET", "/metrics")
    _require(result.status == 401, "operations authentication")


def _assert_agent_inventory() -> None:
    result = _request(
        "GET",
        "/api/v2/operations/agents",
        headers={"X-Operations-Key": OPERATIONS_KEY},
    )
    payload = _json(result)
    _require(
        result.status == 200 and payload.get("count", 0) > 0 and "agents" in payload,
        "v2 operations agent inventory",
    )


def _assert_metrics() -> None:
    result = _request(
        "GET",
        "/metrics",
        headers={"X-Operations-Key": OPERATIONS_KEY},
    )
    body = result.body.decode("utf-8")
    _require(
        result.status == 200
        and "http_requests_total" in body
        and "agent_operations_total" in body,
        "metrics",
    )


def _request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 15,
) -> HttpResult:
    request = Request(
        f"{BASE_URL}{path}",
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return HttpResult(response.status, response.read())
    except HTTPError as exc:
        return HttpResult(exc.code, exc.read())


def _json(result: HttpResult) -> dict[str, object]:
    return json.loads(result.body.decode("utf-8"))


def _require(condition: bool, label: str) -> None:
    if not condition:
        raise SystemExit(f"live smoke failed: {label}")


if __name__ == "__main__":
    main()
