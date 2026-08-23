"""Offline benchmark runner and thesis-friendly report writers."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import platform
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from typing import Any, Literal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import __version__
from app.agent_gateway import AgentGateway
from app.agents import AgentDispatcher, build_default_dispatcher
from app.contracts import AgentError, AgentMessage, AgentResult, TaskStatus
from app.db import session as db_session
from app.db.base import Base
from app.db.seed import seed_database
from app.evaluation.metrics import (
    aggregate_metrics,
    observations_for_categories,
    score_case,
)
from app.evaluation.models import (
    BaselineManifest,
    CaseScore,
    EvalCase,
    EvaluationCategory,
    EvaluationCorpus,
    EvaluationObservation,
    EvaluationReport,
    FailureInjection,
)
from app.mcp.catalog import build_default_mcp_router
from app.orchestrator import MultiAgentOrchestrator

ALLOWED_ACTIONS = frozenset(
    {
        "market.analyze",
        "market.search",
        "product.compare",
        "product.rank",
        "product.search",
        "review.compare",
        "review.summarize",
        "trust.compare",
        "trust.complaints",
    }
)
ALLOWED_INTENTS = frozenset(
    {
        "general.help",
        "general.unsupported",
        "market.analyze",
        "market.search",
        "multi.recommendation",
        "product.compare",
        "product.follow_up",
        "product.rank",
        "product.search",
        "review.summary",
        "trust.complaints",
    }
)
REQUIRED_CATEGORY_COUNT = 4


class FailureInjectingDispatcher(AgentDispatcher):
    """Inject one declared agent failure without changing production agents."""

    def __init__(
        self,
        delegate: AgentDispatcher,
        injection: FailureInjection,
    ) -> None:
        self._delegate = delegate
        self._injection = injection

    async def dispatch(self, message: AgentMessage) -> AgentResult:
        if (
            message.target == self._injection.agent_id
            and message.action == self._injection.action
        ):
            return AgentResult(
                task_id=message.task_id,
                agent_id=self._injection.agent_id,
                status=TaskStatus.FAILED,
                errors=(
                    AgentError(
                        code=self._injection.error_code,
                        message="Agent lỗi có chủ đích trong benchmark offline.",
                        source=self._injection.agent_id,
                        retryable=True,
                    ),
                ),
            )
        return await self._delegate.dispatch(message)

    def agent_ids(self) -> tuple[str, ...]:
        return self._delegate.agent_ids()


def load_corpus(path: Path) -> EvaluationCorpus:
    corpus = EvaluationCorpus.model_validate_json(path.read_text(encoding="utf-8"))
    validate_corpus(corpus)
    return corpus


def load_baseline_manifest(path: Path) -> BaselineManifest:
    return BaselineManifest.model_validate_json(path.read_text(encoding="utf-8"))


def validate_corpus(corpus: EvaluationCorpus) -> None:
    case_ids = [case.case_id for case in corpus.cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("evaluation case IDs must be unique")
    expected_total = len(EvaluationCategory) * REQUIRED_CATEGORY_COUNT
    if len(corpus.cases) != expected_total:
        raise ValueError(
            f"evaluation corpus must contain exactly {expected_total} cases"
        )
    category_counts = Counter(case.category for case in corpus.cases)
    invalid_counts = {
        category.value: category_counts[category]
        for category in EvaluationCategory
        if category_counts[category] != REQUIRED_CATEGORY_COUNT
    }
    if invalid_counts:
        raise ValueError(
            f"evaluation corpus must contain four cases per category: {invalid_counts}"
        )
    unknown_actions = sorted(
        {
            item.action
            for case in corpus.cases
            for item in case.expected_actions
            if item.action not in ALLOWED_ACTIONS
        }
    )
    if unknown_actions:
        raise ValueError(f"unknown canonical actions: {unknown_actions}")
    unknown_intents = sorted(
        {
            intent
            for case in corpus.cases
            for intent in case.accepted_intents
            if intent not in ALLOWED_INTENTS
        }
    )
    if unknown_intents:
        raise ValueError(f"unknown accepted intents: {unknown_intents}")
    for case in corpus.cases:
        if (case.category == EvaluationCategory.TOOL_FAILURE) != (
            case.failure_injection is not None
        ):
            raise ValueError(
                "failure injection must be present only on tool_failure cases: "
                f"{case.case_id}"
            )
        if case.failure_injection is not None:
            matching_count = sum(
                item.count
                for item in case.expected_actions
                if item.action == case.failure_injection.action
            )
            if matching_count != 1:
                raise ValueError(
                    "injected action must occur exactly once in the gold plan: "
                    f"{case.case_id}"
                )


async def run_evaluation(
    *,
    corpus: EvaluationCorpus,
    baseline: BaselineManifest,
    dataset_sha256: str,
    repeats: int = 3,
    max_cases: int | None = None,
    generated_at: datetime | None = None,
) -> EvaluationReport:
    if not 1 <= repeats <= 20:
        raise ValueError("repeats must be between 1 and 20")
    if max_cases is not None and not 1 <= max_cases <= len(corpus.cases):
        raise ValueError("max_cases must select between 1 and the corpus size")
    if baseline.status not in {"baseline_unavailable", "scripted_regression"}:
        raise ValueError("unsupported baseline manifest status")
    cases = corpus.cases[:max_cases] if max_cases is not None else corpus.cases
    if not cases:
        raise ValueError("evaluation requires at least one case")

    observations: list[EvaluationObservation] = []
    scores: list[CaseScore] = []
    with isolated_sample_database() as sample_counts:
        for case in cases:
            for repetition in range(repeats):
                observation = await observe_case(case, repetition=repetition)
                observations.append(observation)
                if repetition == 0:
                    scores.append(score_case(case, observation))

    metrics = aggregate_metrics(scores, observations)
    metrics_by_category: dict[str, dict[str, Any]] = {}
    for category in EvaluationCategory:
        category_scores = [item for item in scores if item.category == category]
        if not category_scores:
            continue
        case_ids = {item.case_id for item in category_scores}
        metrics_by_category[category.value] = aggregate_metrics(
            category_scores,
            observations_for_categories(observations, case_ids),
        )

    comparison_status: Literal[
        "baseline_unavailable",
        "scripted_regression_only",
    ] = (
        "baseline_unavailable"
        if baseline.status == "baseline_unavailable"
        else "scripted_regression_only"
    )
    sut_source_sha256, sut_source_files = _sut_source_manifest()
    return EvaluationReport(
        generated_at=generated_at or datetime.now(UTC),
        application_version=__version__,
        sut_source_sha256=sut_source_sha256,
        sut_source_files=sut_source_files,
        dataset_id=corpus.dataset_id,
        dataset_sha256=dataset_sha256,
        sample_seed_sha256=_seed_sha256(),
        sample_counts=sample_counts,
        python_version=platform.python_version(),
        runtime_platform=platform.platform(),
        repeats=repeats,
        baseline=baseline,
        comparison_status=comparison_status,
        metrics=metrics,
        metrics_by_category=metrics_by_category,
        case_scores=tuple(scores),
        observations=tuple(observations),
        limitations=(
            "Benchmark chỉ dùng 30 sản phẩm và 150 review tổng hợp có gắn nhãn mẫu.",
            (
                "Answer Accuracy là độ chính xác assertion có cấu trúc, "
                "không phải đánh giá ngữ nghĩa tự do."
            ),
            (
                "Latency là thời gian chạy local/offline, không đại diện suy luận "
                "LLM hay hạ tầng production."
            ),
            (
                "Chưa có frozen Phase-1 model baseline đủ provenance nên không "
                "thực hiện so sánh hơn-kém."
            ),
            (
                "Failure injection đo khả năng cô lập lỗi có chủ đích, không mô "
                "phỏng phân phối outage thực tế."
            ),
            (
                "Agent Failure Rate gồm cả lỗi dependency mong đợi ở case thiếu "
                "dữ liệu và lỗi được inject; đây không phải incident rate production."
            ),
        ),
    )


async def observe_case(case: EvalCase, *, repetition: int) -> EvaluationObservation:
    gateway = AgentGateway(router=build_default_mcp_router())
    dispatcher: AgentDispatcher = build_default_dispatcher(gateway)
    if case.failure_injection is not None:
        dispatcher = FailureInjectingDispatcher(dispatcher, case.failure_injection)
    orchestrator = MultiAgentOrchestrator(dispatcher=dispatcher)
    started_at = perf_counter_ns()
    result = await orchestrator.run(
        message=case.message,
        principal_id="evaluation_runner",
        session_id=f"sess_eval_{case.case_id}_{repetition}",
        request_id=f"req_{case.case_id}_{repetition}",
        trace_id=f"trace_{case.case_id}_{repetition}",
    )
    latency_ms = (perf_counter_ns() - started_at) / 1_000_000
    return EvaluationObservation(
        case_id=case.case_id,
        category=case.category,
        repetition=repetition,
        status=result.status,
        predicted_intent=result.intent,
        actions=tuple(step.action for step in result.plan.steps),
        retrieved_product_ids=_retrieved_product_ids(result.agent_results),
        selected_product_id=result.selected_product_id,
        answer=result.answer,
        error_codes=tuple(
            error.code
            for agent_result in result.agent_results
            for error in agent_result.errors
        ),
        provenance_count=len(result.provenance),
        agent_attempts=len(result.agent_results),
        agent_failures=sum(
            item.status == TaskStatus.FAILED for item in result.agent_results
        ),
        latency_ms=latency_ms,
    )


@contextmanager
def isolated_sample_database() -> Iterator[dict[str, int]]:
    """Temporarily bind tools to a fresh SQLite database for one CLI run."""

    with TemporaryDirectory(prefix="ecommerce-evaluation-") as temporary_directory:
        database_path = Path(temporary_directory) / "evaluation.db"
        engine = create_engine(
            f"sqlite+pysqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        session_factory = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )
        previous_session_factory = db_session.SessionLocal
        Base.metadata.create_all(engine)
        db_session.SessionLocal = session_factory
        try:
            yield seed_database(session_factory=session_factory)
        finally:
            db_session.SessionLocal = previous_session_factory
            Base.metadata.drop_all(engine)
            engine.dispose()


def write_report(report: EvaluationReport, output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    payload = report.model_dump(mode="json")
    (output_directory / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_observations_csv(report, output_directory / "observations.csv")
    (output_directory / "report.md").write_text(
        render_markdown_report(report),
        encoding="utf-8",
    )


def render_markdown_report(report: EvaluationReport) -> str:
    lines = [
        "# Báo cáo benchmark offline multi-agent",
        "",
        (
            f"- SUT source manifest SHA-256: `{report.sut_source_sha256}` "
            f"({len(report.sut_source_files)} files)"
        ),
        f"- Dataset: `{report.dataset_id}` (`{report.dataset_sha256}`)",
        f"- Seed source SHA-256: `{report.sample_seed_sha256}`",
        f"- Số case: {len(report.case_scores)}; số lần lặp: {report.repeats}",
        f"- Runtime: Python {report.python_version} trên `{report.runtime_platform}`",
        "- Dữ liệu: **mẫu tổng hợp**, không đại diện thị trường thật",
        f"- Trạng thái so sánh baseline: `{report.comparison_status}`",
        "",
        "## Kết quả tổng hợp",
        "",
        "| Metric | Giá trị | Mẫu | Ghi chú |",
        "| --- | ---: | ---: | --- |",
    ]
    for name, metric in report.metrics.items():
        value = "N/A" if metric.value is None else f"{metric.value:.4f}"
        if metric.numerator is not None and metric.denominator is not None:
            note = f"{metric.numerator:g}/{metric.denominator:g} {metric.unit}"
        else:
            note = metric.unavailable_reason or metric.unit
        lines.append(f"| {name} | {value} | {metric.sample_size} | {note} |")
    lines.extend(
        [
            "",
            "## Tính trung thực của baseline",
            "",
            report.baseline.reason,
            "",
            (
                "Không dùng scripted oracle để thay thế kết quả của một mô hình "
                "single-agent thật."
            ),
            "",
            "## Giới hạn",
            "",
        ]
    )
    lines.extend(f"- {limitation}" for limitation in report.limitations)
    lines.extend(["", "## Kết quả từng case", ""])
    lines.extend(
        f"- `{item.case_id}` ({item.category.value}): "
        f"task={'pass' if item.task_success else 'fail'}, "
        f"route={'pass' if item.routing_correct else 'fail'}, "
        f"plan={'pass' if item.exact_plan else 'fail'}"
        for item in report.case_scores
    )
    return "\n".join(lines) + "\n"


def _write_observations_csv(report: EvaluationReport, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "system_id",
                "case_id",
                "category",
                "repetition",
                "status",
                "predicted_intent",
                "actions",
                "retrieved_product_ids",
                "selected_product_id",
                "error_codes",
                "provenance_count",
                "agent_attempts",
                "agent_failures",
                "latency_ms",
                "token_usage",
                "llm_cost_usd",
            ),
        )
        writer.writeheader()
        for observation in report.observations:
            writer.writerow(
                {
                    "system_id": observation.system_id,
                    "case_id": observation.case_id,
                    "category": observation.category.value,
                    "repetition": observation.repetition,
                    "status": observation.status.value,
                    "predicted_intent": observation.predicted_intent,
                    "actions": "|".join(observation.actions),
                    "retrieved_product_ids": "|".join(
                        str(item) for item in observation.retrieved_product_ids
                    ),
                    "selected_product_id": observation.selected_product_id,
                    "error_codes": "|".join(observation.error_codes),
                    "provenance_count": observation.provenance_count,
                    "agent_attempts": observation.agent_attempts,
                    "agent_failures": observation.agent_failures,
                    "latency_ms": f"{observation.latency_ms:.6f}",
                    "token_usage": observation.token_usage,
                    "llm_cost_usd": observation.llm_cost_usd,
                }
            )


def _retrieved_product_ids(results: Sequence[AgentResult]) -> tuple[int, ...]:
    product_ids: list[int] = []

    def append(value: object) -> None:
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value not in product_ids
        ):
            product_ids.append(value)

    for result in results:
        data = result.data
        for key in ("products", "analyses"):
            values = data.get(key)
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, dict):
                        append(item.get("id", item.get("product_id")))
        for key in ("ranking", "search"):
            container = data.get(key)
            if isinstance(container, dict):
                values = container.get("products")
                if isinstance(values, list):
                    for item in values:
                        if isinstance(item, dict):
                            append(item.get("id"))
        retrieval = data.get("retrieval")
        if isinstance(retrieval, dict):
            product = retrieval.get("product")
            if isinstance(product, dict):
                append(product.get("id"))
    return tuple(product_ids)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_sha256() -> str:
    return _sha256(Path(__file__).resolve().parents[1] / "db" / "seed.py")


def _sut_source_manifest() -> tuple[str, tuple[str, ...]]:
    """Hash ordered app Python paths and contents to bind a report to its SUT."""

    project_root = _default_project_root()
    source_root = project_root / "app"
    source_paths = sorted(
        source_root.rglob("*.py"),
        key=lambda path: path.relative_to(project_root).as_posix(),
    )
    if not source_paths:
        raise RuntimeError("SUT source files are unavailable")

    manifest_digest = hashlib.sha256()
    relative_paths: list[str] = []
    for source_path in source_paths:
        relative_path = source_path.relative_to(project_root).as_posix()
        content_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        manifest_digest.update(f"{relative_path}\0{content_digest}\n".encode("utf-8"))
        relative_paths.append(relative_path)
    return manifest_digest.hexdigest(), tuple(relative_paths)


def _default_project_root() -> Path:
    candidates = (Path.cwd(), Path(__file__).resolve().parents[2])
    for candidate in candidates:
        if (candidate / "evaluation" / "cases.v1.json").is_file():
            return candidate
    return Path.cwd()


def main() -> None:
    project_root = _default_project_root()
    parser = argparse.ArgumentParser(
        description="Run the deterministic sample multi-agent benchmark.",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=project_root / "evaluation" / "cases.v1.json",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=(project_root / "evaluation" / "baselines" / "single_agent.v1.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=project_root / "evaluation" / "results" / "latest",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-cases", type=int, default=None)
    arguments = parser.parse_args()
    corpus = load_corpus(arguments.cases)
    baseline = load_baseline_manifest(arguments.baseline)
    report = asyncio.run(
        run_evaluation(
            corpus=corpus,
            baseline=baseline,
            dataset_sha256=_sha256(arguments.cases),
            repeats=arguments.repeats,
            max_cases=arguments.max_cases,
        )
    )
    write_report(report, arguments.output)
    print(f"Evaluation complete: {len(report.case_scores)} cases")
    print(f"Comparison status: {report.comparison_status}")
    print(f"Report directory: {arguments.output.resolve()}")


if __name__ == "__main__":
    main()
