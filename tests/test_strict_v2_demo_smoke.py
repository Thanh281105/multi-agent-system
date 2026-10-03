"""Counterexamples for the deployed functional gate, without provider calls."""

from __future__ import annotations

import json
from io import BytesIO

import pytest
from pydantic import ValidationError

from app.v2.contracts import ChatResponse
from scripts import ci_live_smoke as smoke
from scripts import strict_v2_demo_smoke as gate


def _completed_response(versions: list[str] | None = None) -> ChatResponse:
    return ChatResponse.model_validate(
        {
            "conversation_id": "conversation_demo",
            "turn": {
                "turn_id": "turn_demo",
                "client_turn_id": "demo:one",
                "status": "completed",
                "outcome": "answered",
                "created_at": "2026-10-03T01:00:00Z",
                "completed_at": "2026-10-03T01:01:00Z",
            },
            "request_id": "request_demo",
            "trace_id": "trace_demo",
            "result": {
                "outcome": "answered",
                "answer": "Đã trả lời.",
                "plan": {
                    "plan_id": "plan_demo",
                    "intent": "knowledge.read",
                    "revisions": [
                        {
                            "revision": 0,
                            "reason": "Initial known-work request",
                            "steps": [
                                {
                                    "step_id": "step_knowledge",
                                    "operation_key": "operation_knowledge",
                                    "capability": "knowledge.retrieve",
                                    "service": "knowledge_service",
                                    "data_version_ids": versions
                                    or ["catalog_demo", "cor_demo", "idx_demo"],
                                }
                            ],
                        }
                    ],
                },
            },
        }
    )


def _sse(*, terminal_count: int = 1, frame_id: str = "2") -> bytes:
    response = _completed_response()
    correlation = {
        "request_id": "request_demo",
        "trace_id": "trace_demo",
        "turn_id": "turn_demo",
    }
    progress = {
        "event": "progress",
        "sequence": 1,
        "phase": "admitted",
        "turn_status": "pending",
        **correlation,
    }
    terminal = {
        "event": "terminal",
        "sequence": 2,
        "server_settled": True,
        "payload": {
            "status": "completed",
            "result": response.result.model_dump(mode="json"),
        },
        **correlation,
    }
    frames = [f"id: 1\nevent: progress\ndata: {json.dumps(progress)}\n\n"]
    for index in range(terminal_count):
        terminal["sequence"] = index + 2
        frames.append(
            f"id: {frame_id if index == 0 else index + 2}\nevent: terminal\n"
            f"data: {json.dumps(terminal)}\n\n"
        )
    return "".join(frames).encode()


def test_strict_gate_refuses_missing_or_partial_published_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("CATALOG_VERSION_ID", "CORPUS_VERSION_ID", "INDEX_MANIFEST_ID"):
        monkeypatch.delenv(f"CI_EXPECTED_{key}", raising=False)
    with pytest.raises(SystemExit, match="all expected published"):
        gate.PublishedPins.from_environment()
    monkeypatch.setenv("CI_EXPECTED_CATALOG_VERSION_ID", "catalog_demo")
    monkeypatch.setenv("CI_EXPECTED_CORPUS_VERSION_ID", "cor_demo")
    with pytest.raises(SystemExit):
        gate.PublishedPins.from_environment()
    monkeypatch.setenv("CI_EXPECTED_INDEX_MANIFEST_ID", "idx_demo")
    assert gate.PublishedPins.from_environment() == gate.PublishedPins(
        "catalog_demo", "cor_demo", "idx_demo"
    )


def test_runtime_unavailable_is_never_a_functional_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "_create_conversation", lambda: "conversation_demo")
    monkeypatch.setattr(
        smoke,
        "_request",
        lambda *args, **kwargs: smoke.HttpResult(
            503, b'{"error":{"code":"v2.runtime_unavailable"}}'
        ),
    )
    with pytest.raises(SystemExit, match="JSON chat success"):
        gate._json_case(
            "Tìm sách",
            {"product.catalog.search"},
            gate.PublishedPins("catalog_demo", "cor_demo", "idx_demo"),
        )


def test_strict_gate_requires_exact_published_versions() -> None:
    pins = gate.PublishedPins("catalog_demo", "cor_demo", "idx_demo")
    gate._assert_bound_result(_completed_response(), pins)
    with pytest.raises(SystemExit, match="published plan bindings"):
        gate._assert_bound_result(
            _completed_response(["catalog_demo", "cor_wrong", "idx_demo"]), pins
        )


def test_answer_without_plan_cannot_skip_published_binding() -> None:
    response = _completed_response()
    assert response.result is not None
    response = response.model_copy(
        update={
            "result": response.result.model_copy(update={"plan": None}),
        }
    )
    with pytest.raises(SystemExit, match="includes published plan bindings"):
        gate._assert_bound_result(
            response, gate.PublishedPins("catalog_demo", "cor_demo", "idx_demo")
        )


def test_cancel_closes_admitted_stream_then_reads_durable_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AdmittedStream(BytesIO):
        status = 200
        headers = {"Content-Type": "text/event-stream"}

    admitted = AdmittedStream(_sse(terminal_count=0))
    monkeypatch.setattr(gate, "urlopen", lambda *args, **kwargs: admitted)
    monkeypatch.setattr(gate, "_create_conversation", lambda: "conversation_demo")
    monkeypatch.setattr(gate, "_request_body", lambda *args: (b"{}", "demo:one"))
    payload = _completed_response().model_dump(mode="json")
    payload["turn"].update(status="cancelled", outcome=None)
    payload["result"] = None
    paths: list[str] = []

    def request(method: str, path: str, **kwargs: object) -> smoke.HttpResult:
        assert admitted.closed
        paths.append(path)
        return smoke.HttpResult(200, json.dumps(payload).encode())

    monkeypatch.setattr(smoke, "_request", request)
    gate._cancel_after_admission()
    assert paths == ["/api/v2/turns/turn_demo/cancel", "/api/v2/turns/turn_demo"]


@pytest.mark.parametrize("terminal_count", [0, 2])
def test_strict_sse_requires_exactly_one_final_terminal(terminal_count: int) -> None:
    with pytest.raises(ValidationError, match="one final terminal"):
        gate._decode_sse(_sse(terminal_count=terminal_count))


def test_strict_sse_validates_wire_ids_and_complete_frames() -> None:
    assert len(gate._decode_sse(_sse()).events) == 2
    with pytest.raises(SystemExit, match="frame identity"):
        gate._decode_sse(_sse(frame_id="wrong"))
    with pytest.raises(SystemExit, match="complete SSE frame"):
        gate._decode_sse(_sse().rstrip())
