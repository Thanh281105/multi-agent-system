import asyncio
import json
from decimal import Decimal
from pathlib import Path

from app.evaluation.rejudge_collect import (
    JudgeCallResult,
    JudgeRequest,
    collect,
)
from app.evaluation.rejudge_cost import BudgetGuard, ModelPrices

PRICES = ModelPrices(Decimal("0.10"), Decimal("0.01"), Decimal("0.50"))


def _requests(n: int) -> dict[str, JudgeRequest]:
    return {f"k{i}": JudgeRequest("ins", "x" * 30, dict, 100) for i in range(n)}


class FakeCall:
    def __init__(self, error: str | None = None) -> None:
        self.keys: list[str] = []
        self.error = error

    async def __call__(self, key: str, request: JudgeRequest) -> JudgeCallResult:
        self.keys.append(key)
        if self.error:
            return JudgeCallResult(None, self.error, 10, 0, 5)
        return JudgeCallResult({"ok": key}, None, 10, 0, 5)


def _run(requests, call, cache: Path, limit: str = "10"):
    return asyncio.run(
        collect(
            requests,
            call=call,
            cache_path=cache,
            guard=BudgetGuard(Decimal(limit)),
            prices=PRICES,
            concurrency=2,
        )
    )


def test_resume_skips_cached_cells(tmp_path: Path) -> None:
    cache = tmp_path / "c.jsonl"
    first = FakeCall()
    _run(_requests(3), first, cache)
    second = FakeCall()
    summary = _run(_requests(3), second, cache)
    assert len(first.keys) == 3
    assert second.keys == []
    assert summary.skipped_cached == 3


def test_budget_cap_stops_before_next_call(tmp_path: Path) -> None:
    call = FakeCall()
    # trần cực thấp: cận trên của một ô đã vượt nên không gọi ô nào
    summary = _run(_requests(3), call, tmp_path / "c.jsonl", limit="0.00001")
    assert call.keys == []
    assert summary.stopped_by_budget is True


def test_invalid_output_recorded_not_retried(tmp_path: Path) -> None:
    cache = tmp_path / "c.jsonl"
    call = FakeCall(error="schema")
    summary = _run(_requests(1), call, cache)
    assert call.keys == ["k0"]
    assert summary.errors == 1
    row = json.loads(cache.read_text(encoding="utf-8").splitlines()[0])
    assert row["error"] == "schema" and row["output"] is None
    again = FakeCall()
    _run(_requests(1), again, cache)
    assert again.keys == []


def test_cache_line_written_incrementally(tmp_path: Path) -> None:
    cache = tmp_path / "c.jsonl"

    class Boom(FakeCall):
        async def __call__(self, key, request):
            if key == "k2":
                raise RuntimeError("hủy giữa chừng")
            return await super().__call__(key, request)

    try:
        asyncio.run(
            collect(
                _requests(3),
                call=Boom(),
                cache_path=cache,
                guard=BudgetGuard(Decimal("10")),
                prices=PRICES,
                concurrency=1,
            )
        )
    except RuntimeError:
        pass
    lines = cache.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["key"] for x in lines] == ["k0", "k1"]


def test_retry_errors_recalls_only_error_cells_and_latest_row_wins(
    tmp_path: Path,
) -> None:
    from app.evaluation.rejudge_collect import read_cache

    cache = tmp_path / "c.jsonl"
    _run(_requests(2), FakeCall(error="boom"), cache)
    plain = FakeCall()
    _run(_requests(2), plain, cache)
    assert plain.keys == []  # mặc định: ô lỗi không bị gọi lại
    retry = FakeCall()
    summary = asyncio.run(
        collect(
            _requests(2),
            call=retry,
            cache_path=cache,
            guard=BudgetGuard(Decimal("10")),
            prices=PRICES,
            concurrency=2,
            retry_errors=True,
        )
    )
    assert sorted(retry.keys) == ["k0", "k1"]
    assert summary.done == 2
    assert all(row["error"] is None for row in read_cache(cache).values())
