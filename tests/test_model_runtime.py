"""Structured model runtime tests without network calls or secret material."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.shared import (
    ModelRuntimeError,
    OpenAIModelRuntime,
    collect_model_calls,
    mark_model_call_fallback,
)


class ParsedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


class FakeResponses:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.requests: list[dict[str, Any]] = []

    async def parse(self, **request: Any) -> Any:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.responses = FakeResponses(outcomes)


def test_required_model_mode_rejects_missing_key() -> None:
    with pytest.raises(ValueError, match="requires OPENAI_API_KEY"):
        Settings(
            _env_file=None,
            model_runtime_mode="required",
            openai_api_key=None,
        )


def parsed_response(answer: str) -> Any:
    return SimpleNamespace(
        status="completed",
        output_parsed=ParsedAnswer(answer=answer),
        usage=SimpleNamespace(input_tokens=12, output_tokens=7, total_tokens=19),
    )


@pytest.mark.asyncio
async def test_model_runtime_returns_schema_and_collects_safe_metadata() -> None:
    client = FakeClient([parsed_response("Có căn cứ.")])
    runtime = OpenAIModelRuntime(
        "test-key",
        client=client,
        max_retries=0,
    )

    with collect_model_calls() as calls:
        result = await runtime.generate_structured(
            stage="synthesis",
            agent_id="orchestrator",
            model="gpt-5.4-mini",
            instructions="Return the requested schema.",
            input_text="bounded facts",
            schema=ParsedAnswer,
        )

    assert result.value.answer == "Có căn cứ."
    assert calls == [result.metadata]
    assert result.metadata.total_tokens == 19
    assert result.metadata.status == "success"
    request = client.responses.requests[0]
    assert request["store"] is False
    assert request["text_format"] is ParsedAnswer
    assert request["reasoning"] == {"effort": "low"}
    assert "test-key" not in repr(request)


@pytest.mark.asyncio
async def test_model_runtime_retries_timeout_then_succeeds() -> None:
    client = FakeClient([TimeoutError(), parsed_response("recovered")])
    runtime = OpenAIModelRuntime(
        "test-key",
        client=client,
        max_retries=1,
    )

    result = await runtime.generate_structured(
        stage="routing",
        agent_id="orchestrator",
        model="gpt-5.4-nano",
        instructions="Classify.",
        input_text="query",
        schema=ParsedAnswer,
    )

    assert result.value.answer == "recovered"
    assert result.metadata.attempts == 2
    assert len(client.responses.requests) == 2


@pytest.mark.asyncio
async def test_model_runtime_rejects_incomplete_or_unparsed_output() -> None:
    client = FakeClient(
        [SimpleNamespace(status="incomplete", output_parsed=None, usage=None)]
    )
    runtime = OpenAIModelRuntime(
        "test-key",
        client=client,
        max_retries=0,
    )

    with collect_model_calls() as calls:
        with pytest.raises(ModelRuntimeError) as captured:
            await runtime.generate_structured(
                stage="planning",
                agent_id="orchestrator",
                model="gpt-5.4-mini",
                instructions="Plan.",
                input_text="request",
                schema=ParsedAnswer,
            )
        updated = mark_model_call_fallback(
            captured.value.metadata,
            "deterministic_fallback",
        )

    assert captured.value.code == "model_response_incomplete"
    assert calls[0] == updated
    assert calls[0].status == "failed"
    assert calls[0].fallback_used is True
    assert calls[0].fallback_reason == "deterministic_fallback"
