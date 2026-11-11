"""Thu thập phán quyết LLM judge có cache, resume và trần chi phí.

Mỗi ô chỉ được gọi tối đa một lần: kết quả (kể cả lỗi) được ghi ngay vào cache
JSONL; lần chạy sau bỏ qua mọi khóa đã có để không phát sinh chi phí lặp.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from app.evaluation.rejudge_cost import BudgetGuard, ModelPrices, call_cost_usd


@dataclass(frozen=True)
class JudgeRequest:
    instructions: str
    input_text: str
    schema: type
    max_output_tokens: int


@dataclass(frozen=True)
class JudgeCallResult:
    output: dict[str, Any] | None
    error: str | None
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int


class JudgeCall(Protocol):
    async def __call__(self, key: str, request: JudgeRequest) -> JudgeCallResult: ...


@dataclass(frozen=True)
class CollectSummary:
    done: int
    skipped_cached: int
    errors: int
    stopped_by_budget: bool
    spent_usd: Decimal


def estimate_max_cost(request: JudgeRequest, prices: ModelPrices) -> Decimal:
    """Cận trên chi phí một ô: input không cache (ký tự/3) + output tối đa."""
    input_tokens = (len(request.instructions) + len(request.input_text)) // 3 + 1
    return call_cost_usd(
        prices,
        input_tokens=input_tokens,
        cached_input_tokens=0,
        output_tokens=request.max_output_tokens,
    )


def read_cache(cache_path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row["key"]] = row
    return rows


async def collect(
    requests: Mapping[str, JudgeRequest],
    *,
    call: JudgeCall,
    cache_path: Path,
    guard: BudgetGuard,
    prices: ModelPrices,
    concurrency: int = 4,
    retry_errors: bool = False,
    validate: Callable[[str, dict[str, Any]], str | None] | None = None,
) -> CollectSummary:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    rows = read_cache(cache_path)

    def _is_bad(key: str, row: Mapping[str, Any]) -> bool:
        if row.get("error"):
            return True
        output = row.get("output")
        return bool(validate and output is not None and validate(key, output))

    cached = {
        key for key, row in rows.items() if not (retry_errors and _is_bad(key, row))
    }
    pending = [key for key in requests if key not in cached]
    semaphore = asyncio.Semaphore(concurrency)
    write_lock = asyncio.Lock()
    state = {"done": 0, "errors": 0, "stopped": False}

    async def one(key: str) -> None:
        async with semaphore:
            request = requests[key]
            upper = estimate_max_cost(request, prices)
            if state["stopped"] or guard.would_exceed(upper):
                state["stopped"] = True
                return
            guard.charge(upper)  # giữ chỗ để các ô đồng thời không cùng vượt trần
            try:
                result = await call(key, request)
            except BaseException:
                guard.charge(-upper)
                raise
            actual = call_cost_usd(
                prices,
                input_tokens=result.input_tokens,
                cached_input_tokens=result.cached_input_tokens,
                output_tokens=result.output_tokens,
            )
            guard.charge(actual - upper)
            error = result.error
            if error is None and validate and result.output is not None:
                error = validate(key, result.output)
            row = {
                "key": key,
                "output": result.output,
                "error": error,
                "input_tokens": result.input_tokens,
                "cached_input_tokens": result.cached_input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": str(actual),
            }
            async with write_lock:
                with cache_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            state["done"] += 1
            if error is not None:
                state["errors"] += 1

    await asyncio.gather(*(one(key) for key in pending))
    return CollectSummary(
        done=state["done"],
        skipped_cached=len(cached & set(requests)),
        errors=state["errors"],
        stopped_by_budget=bool(state["stopped"]),
        spent_usd=guard.spent,
    )


def make_openai_call(
    api_key: str,
    model: str,
    reasoning_effort: Any,
    *,
    max_concurrency: int = 4,
) -> JudgeCall:
    """Gọi Luna qua OpenAIModelRuntime, không retry (tránh nhân chi phí)."""
    from app.shared.model_runtime import (
        ModelRuntimeError,
        OpenAIModelRuntime,
        StructuredModelResult,
        collect_model_calls,
    )

    runtime = OpenAIModelRuntime(
        api_key,
        max_retries=0,
        timeout_seconds=120,
        max_concurrency=max_concurrency,
    )

    async def _call(key: str, request: JudgeRequest) -> JudgeCallResult:
        with collect_model_calls() as calls:
            try:
                result: StructuredModelResult[Any] = await runtime.generate_structured(
                    stage="evaluation.judge",
                    agent_id="model_judge",
                    model=model,
                    instructions=request.instructions,
                    input_text=request.input_text,
                    schema=request.schema,
                    max_output_tokens=request.max_output_tokens,
                    reasoning_effort=reasoning_effort,
                )
                output: dict[str, Any] | None = result.value.model_dump(mode="json")
                error = None
            except ModelRuntimeError as exc:
                output, error = None, str(exc.code)
                if not calls:
                    calls.append(exc.metadata)
        return JudgeCallResult(
            output=output,
            error=error,
            input_tokens=sum(c.input_tokens for c in calls),
            cached_input_tokens=sum(c.cached_input_tokens for c in calls),
            output_tokens=sum(c.output_tokens for c in calls),
        )

    return _call
