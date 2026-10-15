"""Strict public v2 demo gate; run with ``python -m scripts.strict_v2_demo_smoke``.

Requires CI_GATEWAY_KEY and CI_EXPECTED_{CATALOG_VERSION_ID,CORPUS_VERSION_ID,
INDEX_MANIFEST_ID}. Only run against a prepared demo deployment: this creates
conversations and read-only turns, and may call its configured model provider.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import TypeAdapter

from app.v2.contracts import (
    ChatResponse,
    ConversationCreateResponse,
    EvidenceKind,
    TurnResponse,
    TurnSSEEvent,
    TurnSSEProgressEvent,
    TurnSSESequence,
    TurnSSETerminalEvent,
    TurnStatus,
)
from scripts import ci_live_smoke as smoke


@dataclass(frozen=True)
class PublishedPins:
    catalog: str
    corpus: str
    index: str

    @classmethod
    def from_environment(cls) -> PublishedPins:
        values = [
            os.getenv(f"CI_EXPECTED_{name}", "")
            for name in ("CATALOG_VERSION_ID", "CORPUS_VERSION_ID", "INDEX_MANIFEST_ID")
        ]
        smoke._require(
            all(re.fullmatch(r"[a-z][a-z0-9_-]{2,127}", value) for value in values),
            "strict gate requires all expected published catalog/corpus/index pins",
        )
        return cls(*values)


def _create_conversation() -> str:
    result = smoke._request(
        "POST",
        "/api/v2/conversations",
        headers=_headers(),
        body=b'{"mode":"shopper"}',
    )
    smoke._require(result.status == 201, "strict owner-bound conversation creation")
    return ConversationCreateResponse.model_validate_json(
        result.body
    ).conversation.conversation_id


def _headers() -> dict[str, str]:
    return {"X-API-Key": smoke.GATEWAY_KEY, "Content-Type": "application/json"}


def _request_body(conversation_id: str, message: str) -> tuple[bytes, str]:
    client_turn_id = f"demo:{uuid4().hex}"
    return json.dumps(
        {
            "conversation_id": conversation_id,
            "client_turn_id": client_turn_id,
            "message": message,
        },
        ensure_ascii=False,
    ).encode("utf-8"), client_turn_id


def _readback(
    response: TurnResponse, conversation_id: str, client_turn_id: str
) -> None:
    smoke._require(
        response.conversation_id == conversation_id
        and response.turn.client_turn_id == client_turn_id,
        "strict durable turn identity",
    )
    result = smoke._request(
        "GET", f"/api/v2/turns/{response.turn.turn_id}", headers=_headers()
    )
    smoke._require(result.status == 200, "strict durable turn readback")
    durable = TurnResponse.model_validate_json(result.body)
    smoke._require(
        durable.turn == response.turn and durable.result == response.result,
        "strict durable result equals delivered result",
    )


def _assert_bound_result(response: TurnResponse, pins: PublishedPins) -> None:
    smoke._require(
        response.turn.status == TurnStatus.COMPLETED and response.result is not None,
        "strict chat completed (503 or failed turn is never a functional pass)",
    )
    result = response.result
    assert result is not None
    if result.plan is None:
        smoke._require(
            result.outcome.value != "answered",
            "strict answered result includes published plan bindings",
        )
        return
    if result.outcome.value == "answered":
        smoke._require(
            any(revision.steps for revision in result.plan.revisions),
            "strict answered result has a bound read plan",
        )
    for revision in result.plan.revisions:
        for step in revision.steps:
            expected = (
                {pins.catalog, pins.corpus, pins.index}
                if step.capability == "knowledge.retrieve"
                else {pins.catalog}
            )
            smoke._require(
                set(step.data_version_ids) == expected, "strict published plan bindings"
            )


def _json_case(
    message: str, required_capabilities: set[str], pins: PublishedPins
) -> ChatResponse:
    conversation_id = _create_conversation()
    body, client_turn_id = _request_body(conversation_id, message)
    http = smoke._request(
        "POST", "/api/v2/chat", headers=_headers(), body=body, timeout=90
    )
    smoke._require(http.status == 200, "strict JSON chat success")
    response = ChatResponse.model_validate_json(http.body)
    _assert_bound_result(response, pins)
    _readback(response, conversation_id, client_turn_id)
    assert response.result is not None
    completed = {
        item.capability
        for item in response.result.executions
        if item.status.value in {"success", "partial_success"}
    }
    smoke._require(
        required_capabilities <= completed, "strict requested capabilities executed"
    )
    if required_capabilities:
        smoke._require(
            response.result.outcome.value == "answered", "strict use case answered"
        )
        smoke._require(
            bool(response.result.citations), "strict answered case has citations"
        )
    return response


def _decode_sse(body: bytes) -> TurnSSESequence:
    adapter = TypeAdapter(TurnSSEEvent)
    events: list[TurnSSEEvent] = []
    text = body.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    smoke._require(text.endswith("\n\n"), "strict complete SSE frame")
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        data: list[str] = []
        for line in block.splitlines():
            if not line or line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            if name == "data":
                data.append(value.lstrip(" "))
            else:
                fields[name] = value.lstrip(" ")
        if not data:
            continue
        event = adapter.validate_json("\n".join(data))
        smoke._require(
            fields.get("event") == event.event.value
            and fields.get("id") == str(event.sequence),
            "strict SSE frame identity",
        )
        events.append(event)
    return TurnSSESequence(events=tuple(events))


def _knowledge_sse_case(pins: PublishedPins) -> None:
    conversation_id = _create_conversation()
    body, client_turn_id = _request_body(
        conversation_id,
        os.getenv(
            "CI_KNOWLEDGE_SMOKE_MESSAGE", "Nội dung chính của sách Sapiens là gì?"
        ),
    )
    http = smoke._request(
        "POST",
        "/api/v2/chat/stream",
        headers={**_headers(), "Accept": "text/event-stream"},
        body=body,
        timeout=90,
    )
    smoke._require(http.status == 200, "strict SSE HTTP success")
    sequence = _decode_sse(http.body)
    smoke._require(
        any(isinstance(event, TurnSSEProgressEvent) for event in sequence.events),
        "strict SSE admission/progress",
    )
    terminal = sequence.events[-1]
    assert isinstance(terminal, TurnSSETerminalEvent)
    smoke._require(
        terminal.server_settled and terminal.payload.status == TurnStatus.COMPLETED,
        "strict SSE durable completion",
    )
    result = getattr(terminal.payload, "result", None)
    smoke._require(
        result is not None and result.outcome.value == "answered",
        "strict knowledge query answered",
    )
    assert result is not None
    smoke._require(
        any(
            execution.capability == "knowledge.retrieve"
            and execution.status.value in {"success", "partial_success"}
            for execution in result.executions
        ),
        "strict knowledge retrieval executed",
    )
    cited = {citation.evidence_id for citation in result.citations}
    smoke._require(
        any(
            item.kind == EvidenceKind.KNOWLEDGE
            and item.evidence_id in cited
            and item.chunk_id
            and item.span_id
            for item in result.evidence
        ),
        "strict knowledge final exact source/version/chunk/span citation",
    )
    read = smoke._request(
        "GET", f"/api/v2/turns/{terminal.turn_id}", headers=_headers()
    )
    smoke._require(read.status == 200, "strict SSE reconnect readback")
    response = TurnResponse.model_validate_json(read.body)
    smoke._require(
        response.result == result, "strict SSE result persists for reconnect"
    )
    _assert_bound_result(response, pins)
    _readback(response, conversation_id, client_turn_id)


def _cancel_after_admission() -> None:
    conversation_id = _create_conversation()
    body, client_turn_id = _request_body(conversation_id, "Tìm sách Sapiens dưới 300k")
    request = Request(
        f"{smoke.BASE_URL}/api/v2/chat/stream",
        data=body,
        headers={**_headers(), "Accept": "text/event-stream"},
        method="POST",
    )
    deadline = time.monotonic() + 20
    with urlopen(request, timeout=20) as stream:
        smoke._require(
            stream.status == 200
            and stream.headers.get("Content-Type", "").startswith("text/event-stream"),
            "strict cancellable SSE connection",
        )
        data: list[str] = []
        while time.monotonic() < deadline:
            raw_line = stream.readline()
            smoke._require(
                bool(raw_line), "strict cancel stream ended before admission"
            )
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                break
        smoke._require(bool(data), "strict cancel admission deadline")
        event = TypeAdapter(TurnSSEEvent).validate_json("\n".join(data))
        smoke._require(
            isinstance(event, TurnSSEProgressEvent), "strict cancel admitted identity"
        )
        turn_id = event.turn_id
    cancelled = smoke._request(
        "POST", f"/api/v2/turns/{turn_id}/cancel", headers=_headers()
    )
    smoke._require(cancelled.status == 200, "strict durable cancellation")
    response = TurnResponse.model_validate_json(cancelled.body)
    smoke._require(
        response.turn.status in {TurnStatus.CANCELLED, TurnStatus.COMPLETED},
        "strict cancellation settles or preserves raced completion",
    )
    _readback(response, conversation_id, client_turn_id)


def run_functional_gate() -> None:
    pins = PublishedPins.from_environment()
    smoke._require(bool(smoke.GATEWAY_KEY), "CI_GATEWAY_KEY required")
    _json_case("Tìm sách Sapiens dưới 300k", {"product.catalog.search"}, pins)
    compared = _json_case(
        'So sánh "Sapiens" và "Steve Jobs"', {"product.compare"}, pins
    )
    assert compared.result is not None
    cited_comparison = {citation.evidence_id for citation in compared.result.citations}
    smoke._require(
        {
            evidence.source_id
            for evidence in compared.result.evidence
            if evidence.kind == EvidenceKind.CATALOG
            and evidence.evidence_id in cited_comparison
        }
        == {"catalog_product_80", "catalog_product_158"},
        "strict fresh comparison resolves both snapshot titles",
    )
    reviewed = _json_case("Phân tích review của Sapiens", set(), pins)
    assert reviewed.result is not None
    smoke._require(
        reviewed.result.outcome.value == "answered"
        and bool(reviewed.result.citations)
        and any(
            item.capability in {"review.retrieve", "review.compare"}
            and item.status.value in {"success", "partial_success"}
            for item in reviewed.result.executions
        ),
        "strict review analysis answered with executed review evidence",
    )
    _json_case(
        "Gợi ý sách từ 350k đến 400k dựa trên review và độ tin cậy",
        {"product.rank", "review.compare", "trust.compare"},
        pins,
    )
    unsupported = _json_case("Tìm điện thoại Samsung", set(), pins)
    assert unsupported.result is not None
    smoke._require(
        unsupported.result.outcome.value in {"abstained", "needs_clarification"},
        "strict unsupported product refusal",
    )
    _knowledge_sse_case(pins)
    _cancel_after_admission()
    print(
        "Strict functional v2 demo gate passed: fresh use cases, published pins, "
        "JSON/SSE, exact knowledge citations, cancellation, and durable readback."
    )


if __name__ == "__main__":
    run_functional_gate()
