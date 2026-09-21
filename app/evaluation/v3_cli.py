"""Fail-closed executable pilot, resume, and repeat freeze for Evaluation v3."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

from pydantic import BaseModel

from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.v3_checkpoint import (
    CheckpointErrorV3,
    canonical_run_id_v3,
    load_checkpoint_v3,
)
from app.evaluation.v3_executor import (
    EvaluationV3ObservationExecutorFactory,
    build_evaluation_cases_v3,
)
from app.evaluation.v3_gold import (
    LoadedEvaluationGoldV3,
    load_evaluation_gold_v3,
    registry_contract_sha256_v3,
)
from app.evaluation.v3_judge import (
    evaluator_configuration_sha256_v3,
    judge_prompt_sha256_v3,
    model_judge_output_schema_sha256_v3,
)
from app.evaluation.v3_models import (
    EvaluationAssetBindingsV3,
    EvaluationProtocolV3,
    RepeatDecisionV3,
    ScheduledTurnV3,
)
from app.evaluation.v3_protocol import (
    LoadedEvaluationExperimentV3,
    build_evaluation_protocol_v3,
    choose_global_repeat_decision_v3,
    evaluation_protocol_sha256_v3,
    load_evaluation_experiment_v3,
    validate_evaluation_protocol_v3,
)
from app.evaluation.v3_runner import (
    EvaluationCaseV3,
    EvaluationRunResultV3,
    ObservationExecutorFactoryV3,
    execution_case_set_sha256_v3,
    run_observations_v3,
)
from app.evaluation.v3_schedule import (
    build_pilot_schedule_v3,
    pilot_schedule_sha256_v3,
)
from app.evaluation.v3_variant_runtime import (
    PINNED_GENERATION_MODEL_V3,
)
from app.knowledge.v2_contracts import PublishedKnowledgeSnapshot
from app.shared.budget import PricingManifest, default_pricing_manifest_path

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
_PROTOCOL_NAME = "protocol.v3.json"
_SCHEDULE_NAME = "pilot-schedule.v3.json"
_CHECKPOINT_NAME = "pilot-checkpoint.v3.jsonl"
_DECISION_NAME = "repeat-decision.v3.json"

PACKAGE7_JUDGE_PROMPT_V3 = """Score the blinded answer against only the supplied
rubric facts, response constraints, and citation labels. Return every semantic
metric in the required schema order. Do not infer variant identity, reveal hidden
reasoning, add facts outside the rubric, or include free-form rationale."""


class EvaluationV3CLIError(RuntimeError):
    """A stable CLI error that contains no provider or prompt payload."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvaluationV3CLIInputs:
    project_root: Path
    database_url: str | None
    loaded_experiment: LoadedEvaluationExperimentV3
    loaded_gold: LoadedEvaluationGoldV3
    knowledge_snapshot: PublishedKnowledgeSnapshot
    pricing: PricingManifest
    protocol: EvaluationProtocolV3
    cases: Mapping[str, EvaluationCaseV3]


@dataclass(frozen=True, slots=True)
class EvaluationV3CLIPaths:
    output: Path
    protocol: Path
    schedule: Path
    checkpoint: Path
    repeat_decision: Path


def cli(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "validate":
            return _validate_command(arguments)
        if arguments.command == "dry-run":
            return _dry_run_command(arguments)
        if arguments.command in {"pilot", "resume"}:
            return _execute_command(arguments)
        if arguments.command in {"freeze", "repeat-freeze"}:
            return _freeze_command(arguments)
        raise AssertionError(f"unhandled command: {arguments.command}")
    except EvaluationV3CLIError as exc:
        _emit({"status": "error", "error": exc.code, "message": str(exc)})
        return 2
    except CheckpointErrorV3 as exc:
        _emit(
            {
                "status": "error",
                "error": "checkpoint_integrity_error",
                "message": str(exc),
            }
        )
        return 2
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        _emit(
            {
                "status": "error",
                "error": "evaluation_v3_validation_failed",
                "message": _safe_local_message(exc),
            }
        )
        return 2


def main() -> None:
    raise SystemExit(cli())


def _validate_command(arguments: argparse.Namespace) -> int:
    inputs = _load_inputs(arguments)
    protocol_hash = evaluation_protocol_sha256_v3(inputs.protocol)
    if arguments.protocol_file is not None:
        _require_matching_protocol(arguments.protocol_file, inputs.protocol)
    _emit(
        {
            "status": "valid",
            "protocol_sha256": protocol_hash,
            "gold_sha256": inputs.loaded_gold.gold_sha256,
            "split_sha256": inputs.loaded_gold.split_sha256,
            "pilot_cases": len(inputs.protocol.pilot_cases),
            "variants": len(inputs.protocol.variants),
            "default_output": str(
                (inputs.project_root / "output" / "evaluation-v3" / "pilot").resolve()
            ),
        }
    )
    return 0


def _dry_run_command(arguments: argparse.Namespace) -> int:
    inputs = _load_inputs(arguments)
    protocol_hash = evaluation_protocol_sha256_v3(inputs.protocol)
    run_id = _resolve_run_id(arguments, protocol_hash, None)
    schedule = build_pilot_schedule_v3(
        inputs.protocol,
        run_id=run_id,
        protocol_sha256=protocol_hash,
    )
    _emit(
        {
            "status": "dry_run",
            "run_id": run_id,
            "protocol_sha256": protocol_hash,
            "schedule_sha256": pilot_schedule_sha256_v3(schedule),
            "scheduled": len(schedule),
            "warmups": sum(turn.observation_id is None for turn in schedule),
            "measurements": sum(turn.observation_id is not None for turn in schedule),
            "network_calls": 0,
        }
    )
    return 0


def _execute_command(arguments: argparse.Namespace) -> int:
    inputs = _load_inputs(arguments)
    paths = _paths(arguments)
    protocol_hash = evaluation_protocol_sha256_v3(inputs.protocol)
    stored_schedule = (
        _read_schedule(paths.schedule) if paths.schedule.is_file() else None
    )
    run_id = _resolve_run_id(arguments, protocol_hash, stored_schedule)
    schedule = build_pilot_schedule_v3(
        inputs.protocol,
        run_id=run_id,
        protocol_sha256=protocol_hash,
    )

    if arguments.command == "pilot" and paths.checkpoint.exists():
        raise EvaluationV3CLIError(
            "checkpoint_already_exists",
            "pilot refuses an existing checkpoint; use resume",
        )
    if arguments.command == "resume" and not paths.checkpoint.is_file():
        raise EvaluationV3CLIError(
            "checkpoint_missing",
            "resume requires an existing pilot checkpoint",
        )

    if arguments.command == "resume":
        _require_matching_protocol(paths.protocol, inputs.protocol)
        if _read_schedule(paths.schedule) != schedule:
            raise EvaluationV3CLIError(
                "schedule_drift",
                "stored pilot schedule differs from the current frozen schedule",
            )
        state = load_checkpoint_v3(
            paths.checkpoint,
            run_id=run_id,
            protocol_sha256=protocol_hash,
            execution_case_set_sha256=execution_case_set_sha256_v3(
                schedule, inputs.cases
            ),
            schedule_sha256=pilot_schedule_sha256_v3(schedule),
            schedule=schedule,
        )
        if not state.pending_turn_ids and not state.orphan_started_turn_ids:
            result = asyncio.run(
                run_observations_v3(
                    protocol=inputs.protocol,
                    schedule=schedule,
                    checkpoint_path=paths.checkpoint,
                    cases=inputs.cases,
                    executor_factory=_ForbiddenExecutorFactory(),
                )
            )
            _emit_run_result(result, paths.checkpoint)
            return 3 if result.summary.is_partial else 0

    if not arguments.allow_network:
        raise EvaluationV3CLIError(
            "network_consent_required",
            "pilot execution requires explicit --allow-network consent",
        )
    if inputs.database_url is None:
        raise EvaluationV3CLIError(
            "database_url_required",
            "pilot execution requires an explicit --database-url",
        )
    _freeze_or_validate_json(paths.protocol, inputs.protocol, EvaluationProtocolV3)
    _freeze_or_validate_schedule(paths.schedule, schedule)

    try:
        executor_factory = _build_live_executor_factory(inputs)
        result = asyncio.run(
            run_observations_v3(
                protocol=inputs.protocol,
                schedule=schedule,
                checkpoint_path=paths.checkpoint,
                cases=inputs.cases,
                executor_factory=executor_factory,
            )
        )
    except (CheckpointErrorV3, EvaluationV3CLIError):
        raise
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        _emit_partial_environment(
            run_id=run_id,
            protocol=inputs.protocol,
            schedule=schedule,
            cases=inputs.cases,
            checkpoint=paths.checkpoint,
            exc=exc,
        )
        return 3

    _emit_run_result(result, paths.checkpoint)
    return 3 if result.summary.is_partial else 0


def _freeze_command(arguments: argparse.Namespace) -> int:
    inputs = _load_inputs(arguments)
    paths = _paths(arguments)
    _require_matching_protocol(paths.protocol, inputs.protocol)
    stored_schedule = _read_schedule(paths.schedule)
    protocol_hash = evaluation_protocol_sha256_v3(inputs.protocol)
    run_id = _resolve_run_id(arguments, protocol_hash, stored_schedule)
    expected_schedule = build_pilot_schedule_v3(
        inputs.protocol,
        run_id=run_id,
        protocol_sha256=protocol_hash,
    )
    if stored_schedule != expected_schedule:
        raise EvaluationV3CLIError(
            "schedule_drift",
            "stored pilot schedule differs from the current frozen schedule",
        )
    if not paths.checkpoint.is_file():
        raise EvaluationV3CLIError(
            "checkpoint_missing",
            "repeat freeze requires an existing pilot checkpoint",
        )

    case_set_hash = execution_case_set_sha256_v3(expected_schedule, inputs.cases)
    state = load_checkpoint_v3(
        paths.checkpoint,
        run_id=run_id,
        protocol_sha256=protocol_hash,
        execution_case_set_sha256=case_set_hash,
        schedule_sha256=pilot_schedule_sha256_v3(expected_schedule),
        schedule=expected_schedule,
    )
    if (
        state.pending_turn_ids
        or state.missing_turn_ids
        or state.failed_turn_ids
        or state.ambiguous_turn_ids
        or state.orphan_started_turn_ids
    ):
        raise EvaluationV3CLIError(
            "repeat_decision_not_ready",
            "repeat decision requires a complete, unambiguous 8x4 pilot and warmups",
        )

    result = asyncio.run(
        run_observations_v3(
            protocol=inputs.protocol,
            schedule=expected_schedule,
            checkpoint_path=paths.checkpoint,
            cases=inputs.cases,
            executor_factory=_ForbiddenExecutorFactory(),
        )
    )
    decision = choose_global_repeat_decision_v3(
        inputs.protocol,
        result.pilot_cost_evidence,
    )
    _freeze_or_validate_json(paths.repeat_decision, decision, RepeatDecisionV3)
    _emit(
        {
            "status": "frozen",
            "run_id": run_id,
            "protocol_sha256": protocol_hash,
            "selected_repeats": decision.selected_repeats,
            "pilot_effective_cost_usd": str(decision.pilot_effective_cost_usd),
            "projected_benchmark_cost_usd": str(decision.projected_benchmark_cost_usd),
            "repeat_decision": str(paths.repeat_decision.resolve()),
        }
    )
    return 0


def _load_inputs(arguments: argparse.Namespace) -> EvaluationV3CLIInputs:
    project_root = arguments.project_root.resolve()
    loaded_experiment = load_evaluation_experiment_v3(arguments.experiment)
    loaded_gold = load_evaluation_gold_v3(
        arguments.gold,
        arguments.split,
        project_root=project_root,
    )
    knowledge_snapshot = _load_knowledge_snapshot(arguments.runtime_manifest)
    pricing = PricingManifest.load(arguments.pricing)
    assets = _asset_bindings(
        project_root=project_root,
        loaded_experiment=loaded_experiment,
        loaded_gold=loaded_gold,
        knowledge_snapshot=knowledge_snapshot,
        pricing=pricing,
    )
    protocol = build_evaluation_protocol_v3(loaded_experiment, assets=assets)
    cases = build_evaluation_cases_v3(loaded_gold, protocol)
    return EvaluationV3CLIInputs(
        project_root=project_root,
        database_url=getattr(arguments, "database_url", None),
        loaded_experiment=loaded_experiment,
        loaded_gold=loaded_gold,
        knowledge_snapshot=knowledge_snapshot,
        pricing=pricing,
        protocol=protocol,
        cases=cases,
    )


def _asset_bindings(
    *,
    project_root: Path,
    loaded_experiment: LoadedEvaluationExperimentV3,
    loaded_gold: LoadedEvaluationGoldV3,
    knowledge_snapshot: PublishedKnowledgeSnapshot,
    pricing: PricingManifest,
) -> EvaluationAssetBindingsV3:
    evaluator_files = tuple(
        sorted(
            (project_root / "app" / "evaluation").glob("v3_*.py"),
            key=lambda item: item.as_posix(),
        )
    )
    prompt_files = tuple(
        project_root / relative
        for relative in (
            "app/v2/answers.py",
            "app/v2/execution.py",
            "app/v2/planning.py",
            "app/v2/supervisor.py",
            "app/knowledge/grounding.py",
            "app/knowledge/retrieval.py",
        )
    )
    ledger_files = tuple(
        project_root / relative
        for relative in (
            "app/shared/budget.py",
            "app/shared/model_runtime.py",
            "app/models/budget.py",
        )
    )
    source_assets = loaded_gold.gold.source_assets
    evaluator_sha256 = _file_manifest_sha256(project_root, evaluator_files)
    prompt_bundle_sha256 = _file_manifest_sha256(project_root, prompt_files)
    embedding_sha256 = _embedding_sha256(knowledge_snapshot)
    ledger_contract_sha256 = _file_manifest_sha256(project_root, ledger_files)
    scoring_rubric_sha256 = canonical_sha256(
        {
            "category_order": [item.value for item in loaded_gold.gold.category_order],
            "gold_policy": loaded_experiment.config.gold_policy.model_dump(mode="json"),
            "judgment_policy": loaded_experiment.config.judgment_policy.model_dump(
                mode="json"
            ),
            "metrics": [item.value for item in loaded_experiment.config.metrics],
            "required_fact_count": loaded_gold.required_fact_count,
            "schema_version": "3.0",
        }
    )
    judge_prompt_sha256 = judge_prompt_sha256_v3(PACKAGE7_JUDGE_PROMPT_V3)
    judge_schema_sha256 = model_judge_output_schema_sha256_v3()
    model_binding = loaded_experiment.config.judgment_policy.model_binding
    if model_binding is None:
        raise EvaluationV3CLIError(
            "judge_model_binding_missing",
            "Package 7 requires the frozen model-judge binding",
        )
    evaluator_configuration_sha256 = evaluator_configuration_sha256_v3(
        model_binding=model_binding,
        judge_prompt_sha256=judge_prompt_sha256,
        judge_schema_sha256=judge_schema_sha256,
        rubric_sha256=scoring_rubric_sha256,
    )
    return EvaluationAssetBindingsV3(
        evaluator_sha256=evaluator_sha256,
        gold_sha256=loaded_gold.gold_sha256,
        split_sha256=loaded_gold.split_sha256,
        tool_contract_sha256=registry_contract_sha256_v3(),
        prompt_bundle_sha256=prompt_bundle_sha256,
        corpus_sha256=_corpus_sha256(source_assets, knowledge_snapshot),
        index_sha256=_index_sha256(knowledge_snapshot),
        embedding_sha256=embedding_sha256,
        pricing_sha256=pricing.manifest_sha256,
        ledger_contract_sha256=ledger_contract_sha256,
        scoring_rubric_sha256=scoring_rubric_sha256,
        evaluator_configuration_sha256=evaluator_configuration_sha256,
        judge_prompt_sha256=judge_prompt_sha256,
        judge_schema_sha256=judge_schema_sha256,
        embedding_model_dimensions_sha256=embedding_sha256,
        pricing_manifest_sha256=pricing.manifest_sha256,
        usage_ledger_sha256=ledger_contract_sha256,
        rubric_sha256=scoring_rubric_sha256,
    )


def _build_live_executor_factory(
    inputs: EvaluationV3CLIInputs,
) -> ObservationExecutorFactoryV3:
    from app.contracts.a2a import AuthorizationContext
    from app.core.config import Settings
    from app.shared import OpenAIEmbeddingRuntime, OpenAIModelRuntime
    from app.v2.contracts import ConversationMode
    from app.v2.runtime import build_v2_runtime_factory

    config = Settings()
    if inputs.database_url is not None:
        config = config.model_copy(update={"database_url": inputs.database_url})
    api_key = config.openai_api_key_value
    if not api_key:
        raise EvaluationV3CLIError(
            "provider_credential_missing",
            "pilot execution requires a configured OPENAI_API_KEY",
        )
    _validate_runtime_settings(config, inputs.knowledge_snapshot)
    budget = inputs.protocol.experiment.budget
    model_runtime = OpenAIModelRuntime(
        api_key,
        timeout_seconds=budget.attempt_timeout_seconds,
        max_retries=budget.max_retries,
        max_output_tokens=budget.max_output_tokens_per_generation,
        max_concurrency=budget.provider_concurrency,
        circuit_failure_threshold=config.openai_circuit_failure_threshold,
        circuit_recovery_seconds=config.openai_circuit_recovery_seconds,
    )
    embedding_runtime = OpenAIEmbeddingRuntime(
        api_key,
        model=inputs.knowledge_snapshot.embedding_model,
        dimensions=inputs.knowledge_snapshot.embedding_dimension,
        timeout_seconds=budget.attempt_timeout_seconds,
        max_retries=budget.max_retries,
    )
    runtime = build_v2_runtime_factory(
        config,
        model_runtime=model_runtime,
        embedding_runtime=embedding_runtime,
    )
    resolved = runtime.resolve_for_request(
        AuthorizationContext(
            principal_id="evaluation_runner",
            tenant_id="evaluation",
            scopes=frozenset({"ecommerce.read"}),
        ),
        mode=ConversationMode.SHOPPER,
    )
    services = resolved.services
    _validate_live_services(inputs, services)
    services.budget_ledger.create_account(
        account_id=services.budget_account_id,
        hard_limit_nano_usd=100_000_000_000,
        warning_threshold_nano_usd=50_000_000_000,
        max_concurrency=budget.provider_concurrency,
    )
    return EvaluationV3ObservationExecutorFactory(
        services,
        model_runtime=model_runtime,
    )


def _validate_runtime_settings(
    config: Any,
    knowledge_snapshot: PublishedKnowledgeSnapshot,
) -> None:
    if urlsplit(config.database_url).scheme != "postgresql+psycopg":
        raise EvaluationV3CLIError(
            "runtime_database_not_postgresql",
            "pilot execution requires an explicit durable PostgreSQL database",
        )
    expected = (
        "required",
        PINNED_GENERATION_MODEL_V3,
        PINNED_GENERATION_MODEL_V3,
        PINNED_GENERATION_MODEL_V3,
        "openai",
        knowledge_snapshot.embedding_model,
        knowledge_snapshot.embedding_dimension,
        knowledge_snapshot.corpus_version_id,
        knowledge_snapshot.index_manifest_id,
    )
    actual = (
        config.model_runtime_mode,
        config.openai_planning_model,
        config.openai_specialist_model,
        config.openai_synthesis_model,
        config.embedding_backend,
        config.openai_embedding_model,
        config.openai_embedding_dimensions,
        config.v2_corpus_version_id,
        config.v2_index_manifest_id,
    )
    if actual != expected:
        raise EvaluationV3CLIError(
            "runtime_settings_drift",
            "runtime model, embedding, corpus, or index settings differ from protocol",
        )


def _validate_live_services(inputs: EvaluationV3CLIInputs, services: Any) -> None:
    snapshot = services.knowledge_snapshot
    assets = inputs.protocol.assets
    if (
        services.model_snapshot.planning_model != PINNED_GENERATION_MODEL_V3
        or services.model_snapshot.specialist_model != PINNED_GENERATION_MODEL_V3
        or services.model_snapshot.synthesis_model != PINNED_GENERATION_MODEL_V3
        or not services.model_snapshot.provider_runtime_configured
    ):
        _drift("model")
    if services.budget_ledger.manifest.manifest_sha256 != assets.pricing_sha256:
        _drift("pricing")
    if _runtime_registry_sha256(services.read_tools.registry) != (
        assets.tool_contract_sha256
    ):
        _drift("tool_contract")
    _validate_catalog_binding(inputs, services)
    if _corpus_sha256(inputs.loaded_gold.gold.source_assets, snapshot) != (
        assets.corpus_sha256
    ):
        _drift("corpus")
    if _index_sha256(snapshot) != assets.index_sha256:
        _drift("index")
    if _embedding_sha256(snapshot) != assets.embedding_sha256:
        _drift("embedding")


def _runtime_registry_sha256(registry: Any) -> str:
    projection = [
        {
            "capability_id": definition.capability,
            "service": definition.service.value,
            "input_model": definition.input_model.__name__,
            "output_model": definition.output_model.__name__,
            "allowed_modes": sorted(mode.value for mode in definition.allowed_modes),
            "required_scopes": sorted(definition.required_permissions),
            "effect": definition.effect.value,
            "confirmation_policy": definition.confirmation_policy.value,
        }
        for definition in registry.list_capabilities()
    ]
    projection.sort(key=lambda item: str(item["capability_id"]))
    return canonical_sha256(projection)


def _validate_catalog_binding(inputs: EvaluationV3CLIInputs, services: Any) -> None:
    from sqlalchemy import select

    from app.models.dataset_source import DatasetSource

    source_assets = inputs.loaded_gold.gold.source_assets
    with services.session_factory() as session:
        candidates = tuple(
            session.scalars(
                select(DatasetSource).where(
                    DatasetSource.profile == "eval",
                    DatasetSource.products_sha256 == source_assets.products.sha256,
                    DatasetSource.reviews_sha256 == source_assets.reviews.sha256,
                )
            )
        )
    if (
        len(candidates) != 1
        or candidates[0].id not in services.catalog_snapshot.source_ids
    ):
        _drift("catalog")


def _load_knowledge_snapshot(path: Path) -> PublishedKnowledgeSnapshot:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        snapshot = document["snapshot"]
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ) as exc:
        raise EvaluationV3CLIError(
            "runtime_manifest_invalid",
            "knowledge runtime manifest is missing or invalid",
        ) from exc
    return PublishedKnowledgeSnapshot.model_validate(snapshot)


def _corpus_sha256(source_assets: Any, snapshot: PublishedKnowledgeSnapshot) -> str:
    return canonical_sha256(
        {
            "corpus_version_id": snapshot.corpus_version_id,
            "corpus_name": snapshot.corpus_name,
            "corpus_version": snapshot.corpus_version,
            "sources_sha256": source_assets.sources.sha256,
            "mappings_sha256": source_assets.mappings.sha256,
            "catalog_manifest_sha256": source_assets.snapshot_manifest.sha256,
        }
    )


def _index_sha256(snapshot: PublishedKnowledgeSnapshot) -> str:
    normalized = snapshot.model_copy(
        update={"published_at": snapshot.published_at.astimezone(UTC)}
    )
    return canonical_sha256(normalized)


def _embedding_sha256(snapshot: PublishedKnowledgeSnapshot) -> str:
    return canonical_sha256(
        {
            "embedding_model": snapshot.embedding_model,
            "embedding_dimension": snapshot.embedding_dimension,
            "query_embedding_fingerprint": snapshot.query_embedding_fingerprint,
        }
    )


def _file_manifest_sha256(project_root: Path, paths: Sequence[Path]) -> str:
    if not paths:
        raise EvaluationV3CLIError(
            "source_manifest_empty",
            "evaluation source manifest has no files",
        )
    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file():
            raise EvaluationV3CLIError(
                "source_manifest_missing",
                "a protocol-bound source file is missing",
            )
        relative = path.resolve().relative_to(project_root).as_posix()
        content_hash = hashlib.sha256(
            path.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        digest.update(f"{relative}\0{content_hash}\n".encode("utf-8"))
    return digest.hexdigest()


def _resolve_run_id(
    arguments: argparse.Namespace,
    protocol_sha256: str,
    stored_schedule: tuple[ScheduledTurnV3, ...] | None,
) -> str:
    stored_run_id = stored_schedule[0].identity.run_id if stored_schedule else None
    run_id = (
        arguments.run_id
        or stored_run_id
        or canonical_run_id_v3(
            protocol_sha256,
            arguments.run_key,
        )
    )
    if _IDENTIFIER.fullmatch(run_id) is None:
        raise EvaluationV3CLIError("run_id_invalid", "run ID is invalid")
    if stored_run_id is not None and stored_run_id != run_id:
        raise EvaluationV3CLIError(
            "run_id_drift",
            "requested run ID differs from the stored pilot schedule",
        )
    return run_id


def _freeze_or_validate_schedule(
    path: Path,
    schedule: tuple[ScheduledTurnV3, ...],
) -> None:
    if path.exists():
        if _read_schedule(path) != schedule:
            raise EvaluationV3CLIError(
                "schedule_drift",
                "stored pilot schedule differs from the current frozen schedule",
            )
        return
    _exclusive_write(
        path, canonical_json_bytes([item.model_dump(mode="json") for item in schedule])
    )


def _read_schedule(path: Path) -> tuple[ScheduledTurnV3, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise TypeError
        schedule = tuple(ScheduledTurnV3.model_validate(item) for item in payload)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise EvaluationV3CLIError(
            "schedule_invalid",
            "stored pilot schedule is missing or invalid",
        ) from exc
    if not schedule:
        raise EvaluationV3CLIError("schedule_invalid", "stored pilot schedule is empty")
    return schedule


def _require_matching_protocol(path: Path, expected: EvaluationProtocolV3) -> None:
    if not path.is_file():
        raise EvaluationV3CLIError(
            "protocol_missing",
            "a frozen protocol file is required",
        )
    try:
        current = EvaluationProtocolV3.model_validate_json(path.read_text("utf-8"))
        validate_evaluation_protocol_v3(current)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise EvaluationV3CLIError(
            "protocol_invalid",
            "stored protocol is invalid",
        ) from exc
    if current != expected:
        raise EvaluationV3CLIError(
            "protocol_drift",
            "stored protocol differs from current gold, split, model, tool, "
            "or data assets",
        )


def _freeze_or_validate_json[T: BaseModel](
    path: Path,
    value: T,
    model: type[T],
) -> None:
    if path.exists():
        try:
            current = model.model_validate_json(path.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise EvaluationV3CLIError(
                "frozen_artifact_invalid",
                "an existing frozen artifact is invalid",
            ) from exc
        if current != value:
            raise EvaluationV3CLIError(
                "frozen_artifact_drift",
                "an existing frozen artifact differs from the current value",
            )
        return
    _exclusive_write(path, canonical_json_bytes(value))


def _exclusive_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise EvaluationV3CLIError(
            "frozen_artifact_race",
            "a frozen artifact was created concurrently",
        ) from None


def _emit_run_result(result: EvaluationRunResultV3, checkpoint: Path) -> None:
    summary = result.summary
    _emit(
        {
            "status": "partial" if summary.is_partial else "complete",
            "run_id": summary.run_id,
            "protocol_sha256": summary.protocol_sha256,
            "schedule_sha256": summary.schedule_sha256,
            "scheduled": summary.total_scheduled,
            "completed": len(summary.completed_turn_ids),
            "failed": len(summary.failed_turn_ids),
            "ambiguous": len(summary.ambiguous_turn_ids),
            "missing": len(summary.missing_turn_ids),
            "blocked_on_ambiguous_work": summary.blocked_on_ambiguous_work,
            "checkpoint": str(checkpoint.resolve()),
        }
    )


def _emit_partial_environment(
    *,
    run_id: str,
    protocol: EvaluationProtocolV3,
    schedule: tuple[ScheduledTurnV3, ...],
    cases: Mapping[str, EvaluationCaseV3],
    checkpoint: Path,
    exc: BaseException,
) -> None:
    completed = failed = ambiguous = 0
    missing = len(schedule)
    if checkpoint.is_file():
        try:
            state = load_checkpoint_v3(
                checkpoint,
                run_id=run_id,
                protocol_sha256=evaluation_protocol_sha256_v3(protocol),
                execution_case_set_sha256=execution_case_set_sha256_v3(schedule, cases),
                schedule_sha256=pilot_schedule_sha256_v3(schedule),
                schedule=schedule,
            )
            completed = len(state.completed_turn_ids)
            failed = len(state.failed_turn_ids)
            ambiguous = len(state.ambiguous_turn_ids) + len(
                state.orphan_started_turn_ids
            )
            missing = len(state.missing_turn_ids)
        except CheckpointErrorV3:
            pass
    _emit(
        {
            "status": "partial",
            "error": _environment_error_code(exc),
            "run_id": run_id,
            "scheduled": len(schedule),
            "completed": completed,
            "failed": failed,
            "ambiguous": ambiguous,
            "missing": missing,
            "checkpoint": str(checkpoint.resolve()),
        }
    )


class _ForbiddenExecutorFactory:
    def __call__(self, **_: object) -> NoReturn:
        raise EvaluationV3CLIError(
            "repeat_freeze_dispatch_forbidden",
            "repeat freeze attempted to dispatch unfinished work",
        )


def _drift(asset: str) -> NoReturn:
    raise EvaluationV3CLIError(
        f"{asset}_binding_drift",
        f"live {asset} binding differs from the frozen protocol",
    )


def _environment_error_code(exc: BaseException) -> str:
    stable_code = getattr(exc, "code", None)
    if isinstance(stable_code, str) and _IDENTIFIER.fullmatch(stable_code):
        return stable_code
    name = type(exc).__name__.casefold()
    if "budget" in name:
        return "budget_unavailable"
    return "environment_unavailable"


def _safe_local_message(exc: BaseException) -> str:
    module = type(exc).__module__
    if module.startswith("app.evaluation") or module in {
        "builtins",
        "pydantic_core._pydantic_core",
    }:
        return str(exc)[:500]
    return "evaluation v3 validation failed"


def _paths(arguments: argparse.Namespace) -> EvaluationV3CLIPaths:
    output = arguments.output.resolve()
    return EvaluationV3CLIPaths(
        output=output,
        protocol=(arguments.protocol_file or output / _PROTOCOL_NAME),
        schedule=output / _SCHEDULE_NAME,
        checkpoint=(arguments.checkpoint or output / _CHECKPOINT_NAME),
        repeat_decision=(arguments.repeat_decision or output / _DECISION_NAME),
    )


def _parser() -> argparse.ArgumentParser:
    project_root = _default_project_root()
    parser = argparse.ArgumentParser(
        description="Validate, run, resume, and freeze the Package 7 pilot.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate all local v3 bindings")
    _add_inputs(validate, project_root)
    validate.add_argument("--protocol-file", type=Path)

    dry_run = subparsers.add_parser(
        "dry-run", help="build the exact schedule without provider or database calls"
    )
    _add_inputs(dry_run, project_root)
    _add_run_identity(dry_run)

    for name, help_text in (
        ("pilot", "start a new durable 8x4 pilot"),
        ("resume", "resume a durable pilot without redispatch"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        _add_inputs(command, project_root)
        _add_run_identity(command)
        _add_output_paths(command, project_root)
        command.add_argument("--allow-network", action="store_true")
        command.add_argument(
            "--database-url",
            help="explicit durable PostgreSQL URL (never emitted)",
        )

    for name in ("freeze", "repeat-freeze"):
        command = subparsers.add_parser(
            name, help="freeze one global repeat decision from a complete pilot"
        )
        _add_inputs(command, project_root)
        _add_run_identity(command)
        _add_output_paths(command, project_root)
    return parser


def _add_inputs(parser: argparse.ArgumentParser, project_root: Path) -> None:
    parser.add_argument("--project-root", type=Path, default=project_root)
    parser.add_argument(
        "--experiment",
        type=Path,
        default=project_root / "evaluation" / "v3" / "experiment.v3.json",
    )
    parser.add_argument(
        "--gold",
        type=Path,
        default=project_root / "evaluation" / "v3" / "gold.v3.json",
    )
    parser.add_argument(
        "--split",
        type=Path,
        default=project_root / "evaluation" / "v3" / "split.v3.json",
    )
    parser.add_argument(
        "--runtime-manifest",
        type=Path,
        default=project_root
        / "data"
        / "knowledge"
        / "books-v1"
        / "runtime-manifest.json",
    )
    parser.add_argument(
        "--pricing",
        type=Path,
        default=default_pricing_manifest_path(),
    )


def _add_run_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id")
    parser.add_argument("--run-key", default="package7-pilot-v3")


def _add_output_paths(parser: argparse.ArgumentParser, project_root: Path) -> None:
    parser.add_argument(
        "--output",
        "--output-dir",
        dest="output",
        type=Path,
        default=project_root / "output" / "evaluation-v3" / "pilot",
    )
    parser.add_argument("--protocol-file", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--repeat-decision", type=Path)


def _default_project_root() -> Path:
    candidates = (Path.cwd(), Path(__file__).resolve().parents[2])
    for candidate in candidates:
        if (candidate / "evaluation" / "v3" / "experiment.v3.json").is_file():
            return candidate
    return Path.cwd()


def _emit(payload: object) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
