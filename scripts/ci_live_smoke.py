"""Verify the running Compose stack through its public contracts."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
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
    _assert_json_chat()
    _assert_sse_chat()
    _assert_operations_authentication()
    _assert_metrics()
    print("Live stack smoke passed: readiness, JSON, SSE, auth, and metrics")


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
                and checks
                and all(value == "ok" for value in checks.values())
            ):
                return
            last_error = f"status={result.status} body={payload}"
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(5)
    raise SystemExit(f"stack did not become ready: {last_error}")


def _assert_liveness() -> None:
    result = _request("GET", "/livez")
    payload = _json(result)
    _require(result.status == 200 and payload.get("status") == "ok", "liveness")


def _assert_json_chat() -> None:
    result = _request(
        "POST",
        "/api/v1/chat",
        headers={"X-API-Key": GATEWAY_KEY, "Content-Type": "application/json"},
        body=json.dumps(
            {"message": "Tìm tai nghe dưới 1 triệu, bán tốt và ít bị khách phàn nàn."}
        ).encode("utf-8"),
    )
    payload = _json(result)
    _require(
        result.status == 200
        and payload.get("status") in {"success", "partial_success"}
        and payload.get("request_id")
        and payload.get("trace_id")
        and payload.get("provenance"),
        "JSON chat",
    )


def _assert_sse_chat() -> None:
    result = _request(
        "POST",
        "/api/v1/chat/stream",
        headers={
            "Accept": "text/event-stream",
            "X-API-Key": GATEWAY_KEY,
            "Content-Type": "application/json",
        },
        body=json.dumps({"message": "So sánh Nova Air S2 với Sonic G5."}).encode(
            "utf-8"
        ),
        timeout=60,
    )
    _require(result.status == 200, "SSE status")
    events = _parse_sse(result.body)
    names = [name for name, _ in events]
    terminal = [name for name in names if name in {"completed", "error"}]
    _require("status" in names and len(terminal) == 1, "SSE terminal event")
    if terminal == ["completed"]:
        completed = next(payload for name, payload in events if name == "completed")
        _require(
            completed.get("status") in {"success", "partial_success"}, "SSE result"
        )


def _assert_operations_authentication() -> None:
    result = _request("GET", "/metrics")
    _require(result.status == 401, "operations authentication")


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


def _parse_sse(body: bytes) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    event_name: str | None = None
    data_lines: list[str] = []
    for line in body.decode("utf-8").splitlines() + [""]:
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
        elif not line and event_name is not None:
            events.append((event_name, json.loads("".join(data_lines))))
            event_name = None
            data_lines = []
    return events


def _require(condition: bool, label: str) -> None:
    if not condition:
        raise SystemExit(f"live smoke failed: {label}")


if __name__ == "__main__":
    main()
