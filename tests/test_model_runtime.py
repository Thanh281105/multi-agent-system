"""Structured model runtime tests without network calls or secret material."""

from __future__ import annotations

import traceback
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
from app.shared.budget import generation_payload_token_bound
from app.shared.model_runtime import structured_generation_payload_token_bound


class ParsedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


def test_structured_payload_bound_includes_the_provider_schema_envelope() -> None:
    from openai.lib._parsing._responses import type_to_text_format_param

    instructions = "Return the requested schema."
    input_text = "bounded facts"
    expected = generation_payload_token_bound(
        instructions=instructions,
        input_text=input_text,
        text_format=type_to_text_format_param(ParsedAnswer),
    )

    assert (
        structured_generation_payload_token_bound(
            instructions=instructions,
            input_text=input_text,
            schema=ParsedAnswer,
        )
        == expected
    )
    assert expected > len((instructions + input_text).encode("utf-8"))


def test_budgeted_runtime_preflight_uses_the_same_structured_envelope() -> None:
    from openai.lib._parsing._responses import type_to_text_format_param

    class ManifestRecorder:
        service_tier = "standard"

        def __init__(self) -> None:
            self.quoted_input_bound: int | None = None

        def resolve(self, model: str, operation: str) -> None:
            assert model == "gpt-5.4-mini"
            assert operation == "generation"

        def quote(
            self,
            *,
            input_token_bound: int,
            output_token_bound: int,
            **_: Any,
        ) -> None:
            assert output_token_bound == 200
            self.quoted_input_bound = input_token_bound

    manifest = ManifestRecorder()
    budget = SimpleNamespace(ledger=SimpleNamespace(manifest=manifest))
    client = FakeClient([])
    client.max_retries = 0
    runtime = OpenAIModelRuntime("test-key", client=client, max_retries=0)
    instructions = "Return the requested schema."
    input_text = "bounded facts"

    prepared = runtime._prepare_budgeted_request(  # pyright: ignore[reportPrivateUsage]
        budget=budget,
        model="gpt-5.4-mini",
        stage="test",
        agent_id="tester",
        instructions=instructions,
        input_text=input_text,
        schema=ParsedAnswer,
        max_output_tokens=200,
        reasoning_effort="low",
    )

    assert manifest.quoted_input_bound == structured_generation_payload_token_bound(
        instructions=instructions,
        input_text=input_text,
        schema=ParsedAnswer,
    )
    assert prepared.provider_kwargs["text"]["format"] == type_to_text_format_param(
        ParsedAnswer
    )


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


def test_openai_key_is_redacted_until_an_explicit_client_boundary() -> None:
    raw_key = "test-provider-secret-value"
    config = Settings(
        _env_file=None,
        model_runtime_mode="hybrid",
        openai_api_key=raw_key,
    )

    assert config.openai_api_key_value == raw_key
    assert raw_key not in repr(config)
    assert raw_key not in config.model_dump_json()


def parsed_response(answer: str) -> Any:
    return SimpleNamespace(
        id="resp_safe_metadata_123",
        status="completed",
        output_parsed=ParsedAnswer(answer=answer),
        usage=SimpleNamespace(
            input_tokens=12,
            input_tokens_details=SimpleNamespace(cached_tokens=4),
            output_tokens=7,
            output_tokens_details=SimpleNamespace(reasoning_tokens=3),
            total_tokens=19,
        ),
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
    assert result.metadata.cached_input_tokens == 4
    assert result.metadata.reasoning_tokens == 3
    assert result.metadata.response_id == "resp_safe_metadata_123"
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


@pytest.mark.asyncio
async def test_model_runtime_never_exposes_parser_exception_text() -> None:
    canary = "sensitive-provider-payload-fragment"
    runtime = OpenAIModelRuntime(
        "test-key",
        client=FakeClient([ValueError(canary)]),
        max_retries=0,
    )

    with collect_model_calls() as calls:
        with pytest.raises(ModelRuntimeError) as captured:
            await runtime.generate_structured(
                stage="synthesis",
                agent_id="orchestrator",
                model="gpt-5.4-mini",
                instructions="Return the requested schema.",
                input_text="bounded facts",
                schema=ParsedAnswer,
            )

    assert captured.value.code == "model_response_invalid"
    assert captured.value.metadata.error_code == "model_response_invalid"
    serialized = captured.value.metadata.model_dump_json()
    formatted_traceback = "".join(traceback.format_exception(captured.value))
    assert canary not in str(captured.value)
    assert canary not in serialized
    assert canary not in repr(calls)
    assert canary not in formatted_traceback
    assert captured.value.__cause__ is None
