"""Offline provider fakes proving every-attempt runtime accounting."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.budget import ProviderAttempt, ProviderBudgetScope
from app.shared.budget import (
    BudgetCancelledError,
    BudgetDeadlineError,
    BudgetDuplicateAttemptError,
    BudgetLimitExceededError,
    PricingManifest,
    PricingManifestError,
    ProviderBudgetContext,
    SQLProviderBudgetLedger,
    default_pricing_manifest_path,
    provider_budget_scope,
)
from app.shared.embedding_runtime import EmbeddingRuntimeError, OpenAIEmbeddingRuntime
from app.shared.model_runtime import ModelRuntimeError, OpenAIModelRuntime


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


class FakeResponses:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.create_requests: list[dict[str, Any]] = []
        self.parse_requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.create_requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def parse(self, **request: Any) -> Any:
        self.parse_requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeGenerationClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.max_retries = 0
        self.responses = FakeResponses(outcomes)


class EchoResponses:
    def __init__(self) -> None:
        self.create_requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.create_requests.append(request)
        await asyncio.sleep(0)
        return generation_response(str(request["input"]))


class EchoGenerationClient:
    def __init__(self) -> None:
        self.max_retries = 0
        self.responses = EchoResponses()


class FakeEmbeddings:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.requests: list[dict[str, Any]] = []

    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeEmbeddingClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.max_retries = 0
        self.embeddings = FakeAsyncEmbeddings(outcomes)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeAsyncEmbeddings:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def budget_store(
    tmp_path: Path,
) -> Iterator[tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any]]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'runtime-budget.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    ledger = SQLProviderBudgetLedger(
        factory,
        PricingManifest.load(default_pricing_manifest_path()),
    )
    ledger.create_account(account_id="provider-global")
    try:
        yield ledger, factory, engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def add_scope(
    ledger: SQLProviderBudgetLedger,
    scope_id: str,
    purpose: str = "chat",
    **context_options: Any,
) -> ProviderBudgetContext:
    ledger.create_scope(
        scope_id=scope_id,
        account_id="provider-global",
        purpose=purpose,  # type: ignore[arg-type]
    )
    return ProviderBudgetContext(
        ledger=ledger,
        scope_id=scope_id,
        purpose=purpose,  # type: ignore[arg-type]
        **context_options,
    )


def generation_response(
    answer: str = "ok",
    *,
    usage: Any = ...,
    status: str = "completed",
    model: str = "gpt-5.4-mini-2026-03-17",
    service_tier: str = "default",
) -> Any:
    if usage is ...:
        usage = SimpleNamespace(
            input_tokens=20,
            input_tokens_details=SimpleNamespace(cached_tokens=5),
            output_tokens=8,
            output_tokens_details=SimpleNamespace(reasoning_tokens=3),
            total_tokens=28,
        )
    return SimpleNamespace(
        id="resp_budget_test",
        model=model,
        service_tier=service_tier,
        status=status,
        output_text=json.dumps({"answer": answer}),
        usage=usage,
    )


def embedding_response(*, usage: Any = ..., dimensions: int = 1_536) -> Any:
    if usage is ...:
        usage = SimpleNamespace(prompt_tokens=4, total_tokens=4)
    return SimpleNamespace(
        model="text-embedding-3-small",
        usage=usage,
        data=[SimpleNamespace(index=0, embedding=[0.0] * dimensions)],
    )


def read_attempts(
    factory: sessionmaker[Session], scope_id: str
) -> list[ProviderAttempt]:
    with factory() as session:
        return list(
            session.scalars(
                select(ProviderAttempt)
                .where(ProviderAttempt.scope_id == scope_id)
                .order_by(ProviderAttempt.attempt_sequence)
            )
        )


@pytest.mark.asyncio
async def test_budgeted_generation_charges_retry_and_parses_after_usage(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, factory, _ = budget_store
    context = add_scope(ledger, "retry-turn")
    client = FakeGenerationClient([TimeoutError(), generation_response("recovered")])
    runtime = OpenAIModelRuntime("test-key", client=client, max_retries=2)

    async def no_delay(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    with provider_budget_scope(context):
        result = await runtime.generate_structured(
            stage="synthesis",
            agent_id="orchestrator",
            model="gpt-5.4-mini",
            instructions="Return the schema.",
            input_text="bounded evidence",
            schema=Answer,
        )

    assert result.value.answer == "recovered"
    assert result.metadata.attempts == 2
    assert len(client.responses.create_requests) == 2
    assert client.responses.parse_requests == []
    request = client.responses.create_requests[0]
    assert request["service_tier"] == "default"
    assert request["text"]["format"]["type"] == "json_schema"
    attempts = read_attempts(factory, "retry-turn")
    assert [item.attempt_number for item in attempts] == [1, 2]
    assert attempts[0].call_id == attempts[1].call_id
    assert [item.usage_status for item in attempts] == ["unknown", "known"]
    assert [item.result_status for item in attempts] == ["timeout", "success"]
    summary = ledger.scope_usage_summary("retry-turn")
    assert summary.provider_attempts == 2
    assert summary.reasoning_tokens == 3
    assert summary.costs.known_nano_usd > 0
    assert summary.costs.unknown_nano_usd > 0
    with factory() as session:
        scope = session.get(ProviderBudgetScope, "retry-turn")
        assert scope is not None
        assert scope.generation_calls == 1


@pytest.mark.asyncio
async def test_malformed_output_is_charged_before_local_schema_parse(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, factory, _ = budget_store
    context = add_scope(ledger, "malformed-turn")
    response = generation_response()
    response.output_text = "not valid json"
    client = FakeGenerationClient([response])
    runtime = OpenAIModelRuntime("test-key", client=client, max_retries=0)

    with provider_budget_scope(context):
        with pytest.raises(ModelRuntimeError) as captured:
            await runtime.generate_structured(
                stage="synthesis",
                agent_id="orchestrator",
                model="gpt-5.4-mini",
                instructions="Return the schema.",
                input_text="facts",
                schema=Answer,
            )

    assert captured.value.code == "model_response_invalid"
    attempt = read_attempts(factory, "malformed-turn")[0]
    assert attempt.usage_status == "known"
    assert attempt.result_status == "error"
    assert attempt.actual_cost_nano_usd and attempt.actual_cost_nano_usd > 0


@pytest.mark.asyncio
async def test_missing_usage_and_timeout_retain_unknown_reservations(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, factory, _ = budget_store
    missing_context = add_scope(ledger, "missing-usage")
    timeout_context = add_scope(ledger, "timeout-usage")
    success_client = FakeGenerationClient([generation_response(usage=None)])
    timeout_client = FakeGenerationClient([TimeoutError()])

    with provider_budget_scope(missing_context):
        result = await OpenAIModelRuntime(
            "test-key", client=success_client, max_retries=0
        ).generate_structured(
            stage="routing",
            agent_id="orchestrator",
            model="gpt-5.4-mini",
            instructions="Return the schema.",
            input_text="query",
            schema=Answer,
        )
    assert result.value.answer == "ok"
    with provider_budget_scope(timeout_context):
        with pytest.raises(ModelRuntimeError, match="model_timeout"):
            await OpenAIModelRuntime(
                "test-key", client=timeout_client, max_retries=0
            ).generate_structured(
                stage="routing",
                agent_id="orchestrator",
                model="gpt-5.4-mini",
                instructions="Return the schema.",
                input_text="query",
                schema=Answer,
            )
    assert read_attempts(factory, "missing-usage")[0].usage_status == "unknown"
    assert read_attempts(factory, "missing-usage")[0].result_status == "success"
    assert read_attempts(factory, "timeout-usage")[0].usage_status == "unknown"
    assert ledger.scope_summary("missing-usage").unknown_nano_usd > 0
    assert ledger.scope_summary("timeout-usage").unknown_nano_usd > 0


@pytest.mark.asyncio
async def test_cancelled_attempt_is_unknown_and_reraises_cancellation(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, factory, _ = budget_store
    context = add_scope(ledger, "cancelled-turn")
    client = FakeGenerationClient([asyncio.CancelledError()])
    runtime = OpenAIModelRuntime("test-key", client=client, max_retries=0)

    with provider_budget_scope(context):
        with pytest.raises(asyncio.CancelledError):
            await runtime.generate_structured(
                stage="planning",
                agent_id="orchestrator",
                model="gpt-5.4-mini",
                instructions="Return the schema.",
                input_text="query",
                schema=Answer,
            )
    attempt = read_attempts(factory, "cancelled-turn")[0]
    assert (attempt.usage_status, attempt.result_status) == ("unknown", "cancelled")


@pytest.mark.asyncio
async def test_unknown_model_and_duplicate_call_id_reject_before_transport(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, _, _ = budget_store
    client = FakeGenerationClient([generation_response(), generation_response()])
    runtime = OpenAIModelRuntime("test-key", client=client, max_retries=0)
    unknown_context = add_scope(ledger, "unknown-model")
    with provider_budget_scope(unknown_context):
        with pytest.raises(PricingManifestError, match="not_priced"):
            await runtime.generate_structured(
                stage="routing",
                agent_id="orchestrator",
                model="unpriced-model",
                instructions="Return the schema.",
                input_text="query",
                schema=Answer,
            )
    assert client.responses.create_requests == []

    duplicate_context = add_scope(
        ledger,
        "duplicate-call",
        call_id_factory=lambda _: "mcall_cccccccccccccccccccccccccccccccc",
    )
    with provider_budget_scope(duplicate_context):
        await runtime.generate_structured(
            stage="routing",
            agent_id="orchestrator",
            model="gpt-5.4-mini",
            instructions="Return the schema.",
            input_text="query",
            schema=Answer,
        )
        with pytest.raises(BudgetDuplicateAttemptError):
            await runtime.generate_structured(
                stage="routing",
                agent_id="orchestrator",
                model="gpt-5.4-mini",
                instructions="Return the schema.",
                input_text="query",
                schema=Answer,
            )
    assert len(client.responses.create_requests) == 1


@pytest.mark.asyncio
async def test_response_tier_mismatch_retains_unknown_and_surfaces_accounting(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, factory, _ = budget_store
    context = add_scope(ledger, "tier-mismatch")
    client = FakeGenerationClient([generation_response(service_tier="priority")])
    runtime = OpenAIModelRuntime("test-key", client=client, max_retries=0)

    with provider_budget_scope(context):
        with pytest.raises(PricingManifestError, match="pricing_identity"):
            await runtime.generate_structured(
                stage="judge",
                agent_id="judge-agent",
                model="gpt-5.4-mini",
                instructions="Return the schema.",
                input_text="candidate",
                schema=Answer,
            )
    attempt = read_attempts(factory, "tier-mismatch")[0]
    assert attempt.usage_status == "unknown"
    assert attempt.total_tokens == 28
    assert attempt.result_status == "error"


@pytest.mark.asyncio
async def test_budget_context_isolated_across_concurrent_turns(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, factory, _ = budget_store
    first_context = add_scope(ledger, "context-a")
    second_context = add_scope(ledger, "context-b")
    client = EchoGenerationClient()
    runtime = OpenAIModelRuntime("test-key", client=client, max_retries=0)

    async def run(context: ProviderBudgetContext, text: str) -> str:
        with provider_budget_scope(context):
            result = await runtime.generate_structured(
                stage="synthesis",
                agent_id="orchestrator",
                model="gpt-5.4-mini",
                instructions="Return the schema.",
                input_text=text,
                schema=Answer,
            )
            return result.value.answer

    assert await asyncio.gather(
        run(first_context, "first"), run(second_context, "second")
    ) == ["first", "second"]
    assert len(read_attempts(factory, "context-a")) == 1
    assert len(read_attempts(factory, "context-b")) == 1


@pytest.mark.asyncio
async def test_sync_embedding_context_propagates_to_worker_and_retry_is_counted(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, factory, _ = budget_store
    context = add_scope(ledger, "embedding-work", purpose="embedding")
    client = FakeEmbeddingClient([TimeoutError(), embedding_response()])
    runtime = OpenAIEmbeddingRuntime(
        "test-key",
        client=client,
        budget_client_factory=lambda: client,
        dimensions=1_536,
        max_retries=3,
    )

    with provider_budget_scope(context):
        vectors = await asyncio.to_thread(runtime.embed, "văn bản")

    assert len(vectors) == 1_536
    assert client.closed
    assert len(client.embeddings.requests) == 2
    attempts = read_attempts(factory, "embedding-work")
    assert [item.operation for item in attempts] == ["embedding", "embedding"]
    assert [item.usage_status for item in attempts] == ["unknown", "known"]
    assert attempts[0].call_id == attempts[1].call_id


@pytest.mark.asyncio
async def test_budgeted_embedding_enforces_wall_timeout_and_turn_deadline(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, factory, _ = budget_store
    ledger.create_scope(
        scope_id="embedding-wall-timeout",
        account_id="provider-global",
        purpose="embedding",
        deadline_at=datetime.now(UTC) + timedelta(seconds=0.5),
    )
    context = ProviderBudgetContext(
        ledger=ledger,
        scope_id="embedding-wall-timeout",
        purpose="embedding",
        max_retries=1,
        attempt_timeout_seconds=0.4,
    )

    class SlowEmbeddings:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []

        async def create(self, **request: Any) -> Any:
            self.requests.append(request)
            await asyncio.sleep(5)
            return embedding_response()

    client = SimpleNamespace(
        max_retries=0,
        embeddings=SlowEmbeddings(),
        close=lambda: None,
    )
    runtime = OpenAIEmbeddingRuntime(
        "test-key",
        client=client,
        budget_client_factory=lambda: client,
        max_retries=1,
    )

    started = time.monotonic()
    with provider_budget_scope(context):
        with pytest.raises(EmbeddingRuntimeError, match="embedding_provider_failed"):
            await asyncio.to_thread(runtime.embed, "slow input")
    elapsed = time.monotonic() - started

    assert elapsed < 1.5
    assert len(client.embeddings.requests) == 1
    attempt = read_attempts(factory, "embedding-wall-timeout")[0]
    summary = ledger.scope_summary("embedding-wall-timeout")
    assert attempt.result_status == "timeout"
    assert attempt.usage_status == "unknown"
    assert summary.known_nano_usd == 0
    assert summary.reserved_nano_usd == 0
    assert summary.unknown_nano_usd == attempt.reserved_nano_usd


@pytest.mark.asyncio
async def test_budgeted_embedding_cancels_slow_transport_and_retains_unknown(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, factory, _ = budget_store
    cancellation = Event()
    transport_started = Event()
    transport_cancelled = Event()
    context = add_scope(
        ledger,
        "embedding-cancelled",
        purpose="embedding",
        cancellation_requested=cancellation.is_set,
        max_retries=0,
        attempt_timeout_seconds=1,
    )

    class SlowEmbeddings:
        async def create(self, **_: Any) -> Any:
            transport_started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                transport_cancelled.set()
                raise
            return embedding_response()

    client = SimpleNamespace(
        max_retries=0,
        embeddings=SlowEmbeddings(),
        close=lambda: None,
    )
    runtime = OpenAIEmbeddingRuntime(
        "test-key",
        client=client,
        budget_client_factory=lambda: client,
        max_retries=0,
    )

    with provider_budget_scope(context):
        worker = asyncio.create_task(asyncio.to_thread(runtime.embed, "cancel me"))
        assert await asyncio.to_thread(transport_started.wait, 1)
        cancellation.set()
        with pytest.raises(BudgetCancelledError):
            await asyncio.wait_for(worker, timeout=1)

    assert transport_cancelled.is_set()
    attempt = read_attempts(factory, "embedding-cancelled")[0]
    assert attempt.result_status == "cancelled"
    assert attempt.usage_status == "unknown"
    assert (
        ledger.scope_summary("embedding-cancelled").unknown_nano_usd
        == attempt.reserved_nano_usd
    )


@pytest.mark.asyncio
async def test_budgeted_embedding_requires_worker_thread_before_reservation(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, factory, _ = budget_store
    context = add_scope(ledger, "embedding-event-loop", purpose="embedding")
    client = FakeEmbeddingClient([embedding_response()])
    runtime = OpenAIEmbeddingRuntime(
        "test-key",
        client=client,
        budget_client_factory=lambda: client,
    )

    with provider_budget_scope(context):
        with pytest.raises(EmbeddingRuntimeError, match="requires_sync_worker_thread"):
            runtime.embed("query")

    assert read_attempts(factory, "embedding-event-loop") == []
    assert client.embeddings.requests == []


@pytest.mark.asyncio
async def test_embedding_warmup_and_judge_share_global_accounting(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, factory, _ = budget_store
    embedding_context = add_scope(ledger, "embedding", purpose="embedding")
    warmup_context = add_scope(ledger, "warmup", purpose="warmup")
    judge_context = add_scope(ledger, "judge", purpose="judge")
    embedding_client = FakeEmbeddingClient([embedding_response()])
    generation_client = FakeGenerationClient(
        [generation_response("warm"), generation_response("judged")]
    )
    embedding_runtime = OpenAIEmbeddingRuntime(
        "test-key",
        client=embedding_client,
        budget_client_factory=lambda: embedding_client,
        dimensions=1_536,
        max_retries=0,
    )
    generation_runtime = OpenAIModelRuntime(
        "test-key", client=generation_client, max_retries=0
    )

    with provider_budget_scope(embedding_context):
        await asyncio.to_thread(embedding_runtime.embed, "query")
    with provider_budget_scope(warmup_context):
        await generation_runtime.generate_structured(
            stage="warmup",
            agent_id="warmup-agent",
            model="gpt-5.4-mini",
            instructions="Return the schema.",
            input_text="warmup",
            schema=Answer,
        )
    with provider_budget_scope(judge_context):
        await generation_runtime.generate_structured(
            stage="judge",
            agent_id="judge-agent",
            model="gpt-5.4-mini",
            instructions="Return the schema.",
            input_text="judge",
            schema=Answer,
        )

    purposes = {
        read_attempts(factory, scope_id)[0].purpose
        for scope_id in ("embedding", "warmup", "judge")
    }
    assert purposes == {"embedding", "warmup", "judge"}
    assert ledger.account_summary("provider-global").known_nano_usd > 0


def test_budgeted_embedding_rejects_invalid_vector_after_usage_is_charged(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, factory, _ = budget_store
    context = add_scope(ledger, "bad-vector", purpose="embedding")
    client = FakeEmbeddingClient([embedding_response(dimensions=1)])
    runtime = OpenAIEmbeddingRuntime(
        "test-key",
        client=client,
        budget_client_factory=lambda: client,
        dimensions=1_536,
        max_retries=0,
    )

    with provider_budget_scope(context):
        with pytest.raises(EmbeddingRuntimeError, match="dimension"):
            runtime.embed("query")
    attempt = read_attempts(factory, "bad-vector")[0]
    assert attempt.usage_status == "known"
    assert attempt.actual_cost_nano_usd == 80
    assert attempt.result_status == "error"


@pytest.mark.asyncio
async def test_injected_sdk_retries_are_disabled_before_budgeted_http_transport(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, factory, _ = budget_store
    context = add_scope(ledger, "sdk-retries")
    transport_calls = 0

    def fail_once(_: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        return httpx.Response(
            500,
            json={"error": {"message": "synthetic", "type": "server_error"}},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(fail_once))
    sdk_client = AsyncOpenAI(
        api_key="test-key",
        max_retries=2,
        http_client=http_client,
    )
    runtime = OpenAIModelRuntime("test-key", client=sdk_client, max_retries=0)
    try:
        with provider_budget_scope(context):
            with pytest.raises(ModelRuntimeError, match="model_provider_error"):
                await runtime.generate_structured(
                    stage="routing",
                    agent_id="orchestrator",
                    model="gpt-5.4-mini",
                    instructions="Return the schema.",
                    input_text="query",
                    schema=Answer,
                )
    finally:
        await sdk_client.close()
    assert transport_calls == 1
    attempts = read_attempts(factory, "sdk-retries")
    assert len(attempts) == 1
    assert attempts[0].usage_status == "unknown"


@pytest.mark.asyncio
async def test_budgeted_embedding_sdk_attempt_is_one_http_transport(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, factory, _ = budget_store
    context = add_scope(ledger, "embedding-sdk-retries", purpose="embedding")
    transport_calls = 0

    def fail_once(_: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        return httpx.Response(
            500,
            json={"error": {"message": "synthetic", "type": "server_error"}},
        )

    def budget_client_factory() -> AsyncOpenAI:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(fail_once))
        return AsyncOpenAI(
            api_key="test-key",
            max_retries=0,
            http_client=http_client,
        )

    runtime = OpenAIEmbeddingRuntime(
        "test-key",
        client=SimpleNamespace(),
        budget_client_factory=budget_client_factory,
        max_retries=0,
    )
    with provider_budget_scope(context):
        with pytest.raises(EmbeddingRuntimeError, match="embedding_provider_failed"):
            await asyncio.to_thread(runtime.embed, "query")

    assert transport_calls == 1
    attempts = read_attempts(factory, "embedding-sdk-retries")
    assert len(attempts) == 1
    assert attempts[0].usage_status == "unknown"


def test_unscoped_embedding_constructor_does_not_require_retry_clone() -> None:
    class LegacyInjectedClient:
        def __init__(self) -> None:
            self.embeddings = FakeEmbeddings([embedding_response(dimensions=32)])

    client = LegacyInjectedClient()
    runtime = OpenAIEmbeddingRuntime("test-key", client=client, dimensions=32)

    assert len(runtime.embed("legacy")) == 32
    assert len(client.embeddings.requests) == 1


@pytest.mark.asyncio
async def test_scoped_payload_caps_and_expired_deadline_block_transport(
    budget_store: tuple[SQLProviderBudgetLedger, sessionmaker[Session], Any],
) -> None:
    ledger, _, _ = budget_store
    generation_client = FakeGenerationClient([])
    generation_runtime = OpenAIModelRuntime(
        "test-key", client=generation_client, max_retries=5
    )
    context = add_scope(ledger, "generation-caps")
    with provider_budget_scope(context):
        with pytest.raises(BudgetLimitExceededError, match="output_token"):
            await generation_runtime.generate_structured(
                stage="routing",
                agent_id="orchestrator",
                model="gpt-5.4-mini",
                instructions="Return the schema.",
                input_text="query",
                schema=Answer,
                max_output_tokens=1_201,
            )
        with pytest.raises(BudgetLimitExceededError, match="payload_limit"):
            await generation_runtime.generate_structured(
                stage="routing",
                agent_id="orchestrator",
                model="gpt-5.4-mini",
                instructions="Return the schema.",
                input_text="x" * 12_001,
                schema=Answer,
            )
    assert generation_client.responses.create_requests == []
    with pytest.raises(ValueError, match="between 0 and 1"):
        ProviderBudgetContext(
            ledger=ledger,
            scope_id="generation-caps",
            purpose="chat",
            max_retries=3,
        )

    embedding_client = FakeEmbeddingClient([])
    embedding_runtime = OpenAIEmbeddingRuntime(
        "test-key",
        client=embedding_client,
        budget_client_factory=lambda: embedding_client,
        dimensions=1_536,
    )
    embedding_context = add_scope(ledger, "embedding-caps", purpose="embedding")
    with provider_budget_scope(embedding_context):
        with pytest.raises(BudgetLimitExceededError, match="input_token"):
            embedding_runtime.embed("x" * 8_193)
        with pytest.raises(BudgetLimitExceededError, match="request_input"):
            embedding_runtime.embed_many(["x"] * 2_049)
        with pytest.raises(BudgetLimitExceededError, match="request_token"):
            embedding_runtime.embed_many(["x" * 8_120] * 37)
    assert embedding_client.embeddings.requests == []

    clock = [datetime(2026, 9, 9, 12, 0, tzinfo=UTC)]
    ledger._clock = lambda: clock[0]
    ledger.create_scope(
        scope_id="expired-runtime",
        account_id="provider-global",
        purpose="chat",
        deadline_at=clock[0] + timedelta(seconds=5),
    )
    expired_context = ProviderBudgetContext(
        ledger=ledger,
        scope_id="expired-runtime",
        purpose="chat",
    )
    clock[0] += timedelta(seconds=6)
    with provider_budget_scope(expired_context):
        with pytest.raises(BudgetDeadlineError):
            await generation_runtime.generate_structured(
                stage="routing",
                agent_id="orchestrator",
                model="gpt-5.4-mini",
                instructions="Return the schema.",
                input_text="query",
                schema=Answer,
            )
    assert generation_client.responses.create_requests == []
