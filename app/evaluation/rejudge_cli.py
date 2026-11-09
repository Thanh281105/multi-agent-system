"""CLI chấm lại bằng LLM judge: dry-run offline, mẫu nhỏ, chạy đủ, báo cáo.

Thứ tự an toàn: kiểm tra cổng (ngân sách, --allow-network, API key) TRƯỚC khi
nạp dữ liệu hay gọi bất kỳ ô nào. `dry-run` và `report` không bao giờ dùng mạng.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.evaluation.rejudge_collect import (
    JudgeCall,
    JudgeRequest,
    collect,
    estimate_max_cost,
    make_openai_call,
    read_cache,
)
from app.evaluation.rejudge_cost import (
    BudgetGuard,
    RejudgeCostError,
    call_cost_usd,
    load_model_prices,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_P8_ROOT = (
    PROJECT_ROOT
    / "output"
    / "evaluation-v3"
    / "heldout-mini-luna-judge-v3-rating-fix-20261014"
)
DEFAULT_MODEL = "gpt-6-luna"
PRICING_MANIFEST = PROJECT_ROOT / "app" / "shared" / "pricing-manifest.json"
KNOWN_V4_CASES = ("fair_tier2_cross_06", "fair_tier2_cross_07")
EXPECTED_OUTPUT_TOKENS = 300  # chỉ dùng cho ước lượng "thường gặp" của dry-run

CallFactory = Callable[[str, str], JudgeCall]


class CliError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rejudge_cli", description=__doc__)
    parser.add_argument("command", choices=["dry-run", "sample", "run", "report"])
    parser.add_argument("target", choices=["p8", "v4"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--n",
        type=int,
        help="p8: số câu trả lời; v4: số ca (luôn gồm cross_06/07)",
    )
    parser.add_argument("--budget-usd", type=Decimal)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="gọi lại đúng các ô đã lỗi trong cache (mỗi lần chạy thêm một lượt)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--p8-root", type=Path, default=DEFAULT_P8_ROOT)
    parser.add_argument(
        "--v4-results",
        type=Path,
        default=PROJECT_ROOT / "output/evaluation-v4/full_24_benchmark_results.json",
    )
    parser.add_argument(
        "--v4-cases",
        type=Path,
        default=PROJECT_ROOT / "evaluation/v4_fair_benchmark_cases.json",
    )
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=PROJECT_ROOT / "data/snapshots/tiki-books-v4-eval",
    )
    parser.add_argument(
        "--knowledge",
        type=Path,
        default=PROJECT_ROOT / "data/knowledge/books-v1/sources.json",
    )
    return parser


def _cache_path(out: Path, target: str) -> Path:
    return out / f"{target}-judgments.jsonl"


def _build(
    args: argparse.Namespace, *, sample: bool
) -> tuple[dict[str, JudgeRequest], dict[str, Any]]:
    """Dựng request thật cho target; sample=True chọn mẫu xác định (seed 42)."""
    if args.target == "p8":
        from app.evaluation import rejudge_p8

        artifacts = rejudge_p8.load_p8_artifacts(args.p8_root)
        ids = None
        if sample:
            pool = sorted(a.opaque_answer_id for a in artifacts.packet.answers)
            ids = random.Random(42).sample(pool, min(args.n, len(pool)))
        requests = rejudge_p8.build_requests(artifacts, sample=ids)
        return requests, {
            "artifacts": artifacts,
            "instructions_sha256": artifacts.configuration.configuration_sha256,
        }
    from app.evaluation import rejudge_v4

    case_ids = None
    if sample:
        all_ids = sorted(
            c["case_id"]
            for c in json.loads(args.v4_cases.read_text(encoding="utf-8"))["cases"]
        )
        others = [c for c in all_ids if c not in KNOWN_V4_CASES]
        extra = random.Random(42).sample(
            others, max(0, min(args.n - len(KNOWN_V4_CASES), len(others)))
        )
        case_ids = [*KNOWN_V4_CASES, *extra]
    requests = rejudge_v4.build_requests(
        args.v4_results,
        args.v4_cases,
        args.snapshot_dir,
        knowledge_path=args.knowledge,
        case_ids=case_ids,
    )
    return requests, {
        "instructions_sha256": hashlib.sha256(
            rejudge_v4.INSTRUCTIONS.encode("utf-8")
        ).hexdigest()
    }


def _dry_run(args: argparse.Namespace) -> dict[str, Any]:
    prices = load_model_prices(PRICING_MANIFEST, args.model)
    requests, _ = _build(args, sample=False)
    input_tokens = sum(
        (len(r.instructions) + len(r.input_text)) // 3 + 1 for r in requests.values()
    )
    upper = sum((estimate_max_cost(r, prices) for r in requests.values()), Decimal(0))
    typical = call_cost_usd(
        prices,
        input_tokens=input_tokens,
        cached_input_tokens=0,
        output_tokens=EXPECTED_OUTPUT_TOKENS * len(requests),
    )
    return {
        "target": args.target,
        "model": args.model,
        "cells": len(requests),
        "estimated_input_tokens": input_tokens,
        "upper_bound_usd": str(upper),
        "typical_estimate_usd": str(typical),
        "network_calls": 0,
    }


def _require_gates(args: argparse.Namespace) -> str:
    if args.budget_usd is None or args.budget_usd <= 0:
        raise CliError(
            "thiếu --budget-usd > 0: từ chối gọi API không có trần ngân sách"
        )
    if not args.allow_network:
        raise CliError("thiếu --allow-network: lệnh này gọi API trả phí")
    if args.command == "sample" and not args.n:
        raise CliError("sample cần --n")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        from app.core.config import settings

        key = settings.openai_api_key_value
    if not key:
        raise CliError("OPENAI_API_KEY chưa được cấu hình")
    return key


def _p8_validator(
    context: dict[str, Any],
) -> Callable[[str, dict[str, Any]], str | None]:
    """Dùng validator của P8 để ô output không hợp lệ được gọi lại."""
    from app.evaluation import rejudge_p8

    def validate(key: str, output: dict[str, Any]) -> str | None:
        _, errors = rejudge_p8.rejudge_scores(
            context["artifacts"], {key: {"output": output, "error": None}}
        )
        return errors.get(key)

    return validate


def _run(
    args: argparse.Namespace, *, call_factory: CallFactory | None
) -> dict[str, Any]:
    api_key = _require_gates(args)
    prices = load_model_prices(PRICING_MANIFEST, args.model)
    requests, context = _build(args, sample=args.command == "sample")
    validate = _p8_validator(context) if args.target == "p8" else None
    factory = call_factory or (
        lambda key, model: make_openai_call(
            key, model, "none", max_concurrency=args.concurrency
        )
    )
    guard = BudgetGuard(args.budget_usd)
    summary = asyncio.run(
        collect(
            requests,
            call=factory(api_key, args.model),
            cache_path=_cache_path(args.out, args.target),
            guard=guard,
            prices=prices,
            concurrency=args.concurrency,
            retry_errors=args.retry_errors,
            validate=validate,
        )
    )
    return {
        "target": args.target,
        "cells": len(requests),
        "done": summary.done,
        "skipped_cached": summary.skipped_cached,
        "errors": summary.errors,
        "stopped_by_budget": summary.stopped_by_budget,
        "spent_usd": str(summary.spent_usd),
        "budget_usd": str(args.budget_usd),
    }


def _git_head() -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _totals(rows: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "rows": len(rows),
        "errors": sum(1 for r in rows.values() if r.get("error") is not None),
        "input_tokens": sum(r.get("input_tokens", 0) for r in rows.values()),
        "cached_input_tokens": sum(
            r.get("cached_input_tokens", 0) for r in rows.values()
        ),
        "output_tokens": sum(r.get("output_tokens", 0) for r in rows.values()),
        "cost_usd": str(
            sum((Decimal(r.get("cost_usd", "0")) for r in rows.values()), Decimal(0))
        ),
    }


def _report(args: argparse.Namespace) -> dict[str, Any]:
    cache_path = _cache_path(args.out, args.target)
    rows = read_cache(cache_path)
    if not rows:
        raise CliError(f"cache rỗng hoặc không tồn tại: {cache_path}")
    if args.target == "p8":
        from app.evaluation import rejudge_p8

        artifacts = rejudge_p8.load_p8_artifacts(args.p8_root)
        scores, errors = rejudge_p8.rejudge_scores(artifacts, rows)
        comparison = rejudge_p8.compare_runs(artifacts, scores)
        content: dict[str, Any] = {
            "comparison": json.loads(json.dumps(comparison, default=vars)),
            "invalid_or_error_rows": errors,
        }
        instructions_sha = artifacts.configuration.configuration_sha256
    else:
        from app.evaluation import rejudge_v4

        content = rejudge_v4.summarize(args.v4_results, args.v4_cases, rows)
        instructions_sha = hashlib.sha256(
            rejudge_v4.INSTRUCTIONS.encode("utf-8")
        ).hexdigest()
    args.out.mkdir(parents=True, exist_ok=True)
    report_path = args.out / f"{args.target}-report.json"
    report_path.write_text(
        json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest = {
        "target": args.target,
        "model": args.model,
        "exploratory": True,
        "git_head": _git_head(),
        "instructions_sha256": instructions_sha,
        "totals": _totals(rows),
        "files": {
            p.name: _sha256_file(p)
            for p in sorted(args.out.iterdir())
            if p.is_file() and p.name != f"{args.target}-manifest.json"
        },
    }
    (args.out / f"{args.target}-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(
    argv: Sequence[str] | None = None, *, call_factory: CallFactory | None = None
) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "dry-run":
            result = _dry_run(args)
        elif args.command == "report":
            result = _report(args)
        else:
            result = _run(args, call_factory=call_factory)
    except (CliError, RejudgeCostError) as exc:
        print(f"LỖI: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
