"""Evaluation v2 protocol, statistics, robustness, cost, and artifact tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.contracts import TaskStatus
from app.evaluation.artifacts import validate_bundle, write_bundle
from app.evaluation.comparison import compare_variants, summarize_robustness
from app.evaluation.protocol import (
    canonical_sha256,
    estimate_model_call_cost,
    estimate_observation_cost,
    protocol_sha256,
    validate_observation_protocol,
)
from app.evaluation.v2_models import (
    ComparisonMetric,
    EvaluationExperimentConfigV2,
    EvaluationObservationV2,
    EvaluationPhase,
    EvaluationProtocolV2,
    EvaluationVariantV2,
    ModelBindingV2,
    ModelCallOutcome,
    ModelCallV2,
    ModelPriceV2,
    ModelStage,
    PricingManifestV2,
    RobustnessPolicy,
    RuntimeMode,
    TokenUsageV2,
)


def test_protocol_hash_is_canonical_and_variants_are_strict() -> None:
    protocol = _protocol()

    assert protocol_sha256(protocol) == canonical_sha256(protocol)
    assert len(protocol_sha256(protocol)) == 64
    assert protocol.variants[1].parent_variant_id == "deterministic_v1"

    with pytest.raises(ValidationError, match="deterministic variants"):
        EvaluationVariantV2.model_validate(
            {
                **_deterministic_variant().model_dump(),
                "model_router_enabled": True,
                "max_model_calls": 1,
            }
        )
    with pytest.raises(ValidationError, match="unknown parent variant"):
        EvaluationProtocolV2.model_validate(
            {
                **protocol.model_dump(),
                "variants": [
                    _deterministic_variant().model_dump(),
                    {
                        **_hybrid_variant().model_dump(),
                        "parent_variant_id": "unknown_variant",
                    },
                ],
            }
        )
    original_hash = protocol_sha256(protocol)
    with pytest.raises(ValidationError, match="frozen"):
        protocol.variants[1].model_bindings[0].model = "mutated-model"
    assert protocol_sha256(protocol) == original_hash


def test_experiment_config_rejects_ambiguous_warmups_and_parent_cycles() -> None:
    variants = (_deterministic_variant(), _hybrid_variant())
    config = EvaluationExperimentConfigV2(
        experiment_id="paired_experiment_v2",
        baseline_variant_id="deterministic_v1",
        warmup_repeats=1,
        warmup_case_id="case_a",
        variants=variants,
    )

    assert config.baseline_variant_id == "deterministic_v1"
    with pytest.raises(ValidationError, match="declared together"):
        EvaluationExperimentConfigV2.model_validate(
            {
                **config.model_dump(),
                "warmup_case_id": None,
            }
        )
    cyclic = (
        variants[0].model_copy(update={"parent_variant_id": "hybrid_full"}),
        variants[1],
    )
    with pytest.raises(ValidationError, match="acyclic"):
        EvaluationExperimentConfigV2.model_validate(
            {
                **config.model_dump(),
                "variants": [item.model_dump() for item in cyclic],
            }
        )


def test_latency_subset_is_protocol_bound_and_bundle_complete(tmp_path: Path) -> None:
    protocol = EvaluationProtocolV2.model_validate(
        {
            **_protocol().model_dump(),
            "latency_repeats": 1,
            "latency_case_order": ["case_a", "case_c"],
        }
    )
    correctness = _paired_observations(protocol)
    latency = tuple(
        _observation(
            protocol,
            variant_id=variant_id,
            case_id=case_id,
            phase=EvaluationPhase.LATENCY,
            execution_order=len(correctness) + index,
        )
        for index, (case_id, variant_id) in enumerate(
            (
                ("case_a", "deterministic_v1"),
                ("case_a", "hybrid_full"),
                ("case_c", "deterministic_v1"),
                ("case_c", "hybrid_full"),
            )
        )
    )

    manifest = write_bundle(
        tmp_path / "latency_subset",
        run_id="run_paired_v2",
        protocol=protocol,
        observations=(*correctness, *latency),
        comparisons=(),
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )

    assert manifest.observation_count == 12
    invalid_latency = latency[0].model_copy(update={"case_id": "case_b"})
    with pytest.raises(ValueError, match="non-latency case"):
        validate_observation_protocol(invalid_latency, protocol)


def test_zero_attempt_model_call_is_reserved_for_open_circuit() -> None:
    call = ModelCallV2(
        call_id="call_router_circuit",
        stage=ModelStage.ROUTING,
        provider="openai",
        model="gpt-5.4-nano",
        outcome=ModelCallOutcome.ERROR,
        attempt=0,
        latency_ms=0,
        error_code="model_circuit_open",
    )

    assert call.attempt == 0
    with pytest.raises(ValidationError, match="open circuit"):
        ModelCallV2.model_validate(
            {
                **call.model_dump(),
                "error_code": "model_timeout",
            }
        )


def test_token_accounting_and_versioned_cost_are_exact() -> None:
    usage = TokenUsageV2(
        input_tokens=1_000,
        cached_input_tokens=200,
        output_tokens=500,
        reasoning_tokens=100,
        total_tokens=1_500,
    )
    call = _model_call(usage=usage)
    pricing = _pricing()

    estimate = estimate_model_call_cost(call, pricing)

    assert estimate.value_usd == Decimal("0.002820000000")
    assert estimate_observation_cost(
        _observation(
            _protocol(),
            variant_id="hybrid_full",
            case_id="case_a",
            model_calls=(call,),
        ),
        pricing,
    ).value_usd == Decimal("0.002820000000")
    assert estimate_observation_cost(
        _observation(_protocol(), variant_id="deterministic_v1", case_id="case_a"),
        pricing,
    ).value_usd == Decimal("0E-12")

    with pytest.raises(ValidationError, match="total tokens"):
        TokenUsageV2(
            input_tokens=10,
            output_tokens=5,
            total_tokens=14,
        )
    missing = estimate_model_call_cost(
        call.model_copy(update={"model": "unpriced-model"}),
        pricing,
    )
    assert missing.value_usd is None
    assert "no entry" in (missing.unavailable_reason or "")


def test_paired_comparison_is_case_clustered_and_reproducible() -> None:
    protocol = _protocol()
    observations = _paired_observations(protocol)

    first = compare_variants(
        observations,
        baseline_variant_id="deterministic_v1",
        candidate_variant_id="hybrid_full",
        metric=ComparisonMetric.TASK_SUCCESS,
        phase=EvaluationPhase.CORRECTNESS,
        bootstrap_samples=1_000,
        random_seed=73,
    )
    second = compare_variants(
        observations,
        baseline_variant_id="deterministic_v1",
        candidate_variant_id="hybrid_full",
        metric=ComparisonMetric.TASK_SUCCESS,
        phase=EvaluationPhase.CORRECTNESS,
        bootstrap_samples=1_000,
        random_seed=73,
    )

    assert first == second
    assert first.observation_pair_count == first.case_count == 4
    assert first.baseline_mean == 0.5
    assert first.candidate_mean == 0.75
    assert first.mean_delta == 0.25
    assert first.win_tie_loss.model_dump() == {"wins": 2, "ties": 1, "losses": 1}
    assert first.exact_two_sided_p_value == 1
    assert first.confidence_interval.bootstrap_samples == 1_000


def test_paired_comparison_honors_lower_is_better_and_rejects_mismatch() -> None:
    protocol = _protocol()
    observations = _paired_observations(protocol)
    comparison = compare_variants(
        observations,
        baseline_variant_id="deterministic_v1",
        candidate_variant_id="hybrid_full",
        metric=ComparisonMetric.END_TO_END_LATENCY_MS,
        phase=EvaluationPhase.CORRECTNESS,
        bootstrap_samples=200,
    )

    assert comparison.direction.value == "lower_is_better"
    assert comparison.win_tie_loss.wins == 4
    assert comparison.mean_delta == -10

    with pytest.raises(ValueError, match="different case/repetition keys"):
        compare_variants(
            observations[:-1],
            baseline_variant_id="deterministic_v1",
            candidate_variant_id="hybrid_full",
            metric=ComparisonMetric.TASK_SUCCESS,
            phase=EvaluationPhase.CORRECTNESS,
            bootstrap_samples=200,
        )


def test_comparisons_keep_correctness_and_latency_phases_separate() -> None:
    protocol = EvaluationProtocolV2.model_validate(
        {**_protocol().model_dump(), "latency_repeats": 1}
    )
    correctness = _paired_observations(protocol)
    latency = tuple(
        item.model_copy(
            update={
                "phase": EvaluationPhase.LATENCY,
                "end_to_end_latency_ms": (
                    500 if item.variant_id == "deterministic_v1" else 700
                ),
            }
        )
        for item in correctness
    )
    observations = (*correctness, *latency)

    quality = compare_variants(
        observations,
        baseline_variant_id="deterministic_v1",
        candidate_variant_id="hybrid_full",
        metric=ComparisonMetric.TASK_SUCCESS,
        phase=EvaluationPhase.CORRECTNESS,
        bootstrap_samples=200,
    )
    speed = compare_variants(
        observations,
        baseline_variant_id="deterministic_v1",
        candidate_variant_id="hybrid_full",
        metric=ComparisonMetric.END_TO_END_LATENCY_MS,
        phase=EvaluationPhase.LATENCY,
        bootstrap_samples=200,
    )

    assert quality.mean_delta == 0.25
    assert speed.mean_delta == 200
    assert speed.win_tie_loss.losses == 4


def test_robustness_summary_pairs_transforms_with_clean_parents() -> None:
    protocol = _robustness_protocol()
    observations = _robustness_observations(protocol)

    summary = summarize_robustness(observations, variant_id="hybrid_full")

    assert summary.clean_case_count == 2
    assert summary.transformed_case_count == 4
    assert summary.task_success_rate == 0.75
    assert summary.intent_consistency_rate == 0.75
    assert summary.mean_task_success_degradation == 0.25
    assert summary.maximum_task_success_degradation == 0.5
    assert summary.worst_transform_id == "typo_noise"

    extra = _observation(
        protocol,
        variant_id="hybrid_full",
        case_id="clean_a_typo_alt",
        robustness_policy=RobustnessPolicy.LABEL_PRESERVING,
        parent_case_id="clean_a",
        transform_id="typo_noise",
    )
    expanded = summarize_robustness((*observations, extra), variant_id="hybrid_full")
    assert expanded.transformed_case_count == 5
    assert (
        next(
            item for item in expanded.transforms if item.transform_id == "typo_noise"
        ).case_count
        == 3
    )


def test_artifact_bundle_is_atomic_hashed_and_non_overwriting(tmp_path: Path) -> None:
    protocol = _protocol()
    observations = _paired_observations(protocol)
    comparison = compare_variants(
        observations,
        baseline_variant_id="deterministic_v1",
        candidate_variant_id="hybrid_full",
        metric=ComparisonMetric.TASK_SUCCESS,
        phase=EvaluationPhase.CORRECTNESS,
        bootstrap_samples=200,
    )
    output = tmp_path / "run_v2"

    manifest = write_bundle(
        output,
        run_id="run_paired_v2",
        protocol=protocol,
        observations=observations,
        comparisons=(comparison,),
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )

    assert manifest.observation_count == 8
    assert manifest.comparison_count == 1
    assert validate_bundle(output) == manifest
    assert {entry.path for entry in manifest.files} == {
        "protocol.json",
        "observations.jsonl",
        "comparisons.json",
        "robustness.json",
        "report.json",
    }
    with pytest.raises(FileExistsError):
        write_bundle(
            output,
            run_id="run_paired_v2",
            protocol=protocol,
            observations=observations,
            comparisons=(comparison,),
            created_at=datetime(2026, 8, 25, tzinfo=UTC),
        )


def test_artifact_validator_detects_tampering_and_unexpected_files(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    observations = _paired_observations(protocol)
    output = tmp_path / "run_v2"
    write_bundle(
        output,
        run_id="run_paired_v2",
        protocol=protocol,
        observations=observations,
        comparisons=(),
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )

    observations_path = output / "observations.jsonl"
    observations_path.write_text(
        observations_path.read_text(encoding="utf-8") + "{}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="size mismatch"):
        validate_bundle(output)

    second_output = tmp_path / "run_v2_clean"
    write_bundle(
        second_output,
        run_id="run_paired_v2",
        protocol=protocol,
        observations=observations,
        comparisons=(),
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )
    (second_output / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected files"):
        validate_bundle(second_output)


def test_complete_bundle_rejects_partial_matrix_and_duplicate_order(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    observations = _paired_observations(protocol)

    with pytest.raises(ValueError, match="observation matrix"):
        write_bundle(
            tmp_path / "partial",
            run_id="run_paired_v2",
            protocol=protocol,
            observations=observations[:-1],
            comparisons=(),
            created_at=datetime(2026, 8, 25, tzinfo=UTC),
        )
    duplicate_order = observations[-1].model_copy(update={"execution_order": 0})
    with pytest.raises(ValueError, match="execution orders must be unique"):
        write_bundle(
            tmp_path / "duplicate_order",
            run_id="run_paired_v2",
            protocol=protocol,
            observations=(*observations[:-1], duplicate_order),
            comparisons=(),
            created_at=datetime(2026, 8, 25, tzinfo=UTC),
        )


def test_bundle_binds_versioned_pricing_to_protocol(tmp_path: Path) -> None:
    pricing = _pricing()
    protocol = EvaluationProtocolV2.model_validate(
        {
            **_protocol().model_dump(),
            "pricing_sha256": canonical_sha256(pricing),
        }
    )
    observations = tuple(
        item.model_copy(update={"estimated_cost_usd": Decimal("0E-12")})
        for item in _paired_observations(protocol)
    )
    output = tmp_path / "priced"

    manifest = write_bundle(
        output,
        run_id="run_paired_v2",
        protocol=protocol,
        observations=observations,
        comparisons=(),
        pricing=pricing,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )

    assert "pricing.json" in {entry.path for entry in manifest.files}
    assert validate_bundle(output) == manifest
    fraudulent = observations[0].model_copy(
        update={"estimated_cost_usd": Decimal("999")}
    )
    with pytest.raises(ValueError, match="does not match pinned pricing"):
        write_bundle(
            tmp_path / "fraudulent_cost",
            run_id="run_paired_v2",
            protocol=protocol,
            observations=(fraudulent, *observations[1:]),
            comparisons=(),
            pricing=pricing,
            created_at=datetime(2026, 8, 25, tzinfo=UTC),
        )


def test_bundle_preserves_a_recomputed_cost_unavailability_reason(
    tmp_path: Path,
) -> None:
    pricing = _pricing()
    protocol = EvaluationProtocolV2.model_validate(
        {
            **_protocol().model_dump(),
            "pricing_sha256": canonical_sha256(pricing),
        }
    )
    observations = list(_paired_observations(protocol))
    failed_call = _model_call().model_copy(
        update={
            "outcome": ModelCallOutcome.ERROR,
            "error_code": "model_timeout",
        }
    )
    unavailable = estimate_observation_cost(
        observations[1].model_copy(update={"model_calls": (failed_call,)}),
        pricing,
    )
    observations = [
        item.model_copy(update={"estimated_cost_usd": Decimal("0E-12")})
        for item in observations
    ]
    observations[1] = observations[1].model_copy(
        update={
            "model_calls": (failed_call,),
            "estimated_cost_usd": None,
            "cost_unavailable_reason": unavailable.unavailable_reason,
        }
    )

    manifest = write_bundle(
        tmp_path / "unavailable_cost",
        run_id="run_paired_v2",
        protocol=protocol,
        observations=observations,
        comparisons=(),
        pricing=pricing,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )

    assert manifest.observation_count == 8
    assert unavailable.unavailable_reason == (
        "model call 'call_synthesis_1' has no token usage"
    )
    with pytest.raises(ValueError, match="declare pricing together"):
        write_bundle(
            tmp_path / "missing_pricing",
            run_id="run_paired_v2",
            protocol=protocol,
            observations=observations,
            comparisons=(),
            created_at=datetime(2026, 8, 25, tzinfo=UTC),
        )


def test_non_finite_metrics_and_noncanonical_nan_are_rejected() -> None:
    observation = _observation(
        _protocol(),
        variant_id="hybrid_full",
        case_id="case_a",
    )

    with pytest.raises(ValidationError, match="finite number"):
        EvaluationObservationV2.model_validate(
            {**observation.model_dump(), "end_to_end_latency_ms": float("nan")}
        )
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_sha256({"metric": float("nan")})


def test_observation_must_bind_to_protocol_and_valid_stage_latency() -> None:
    protocol = _protocol()
    observation = _observation(
        protocol,
        variant_id="hybrid_full",
        case_id="case_a",
    )

    validate_observation_protocol(observation, protocol)
    with pytest.raises(ValueError, match="protocol hash"):
        validate_observation_protocol(
            observation.model_copy(update={"protocol_sha256": "f" * 64}),
            protocol,
        )
    with pytest.raises(ValidationError, match="stage latency"):
        EvaluationObservationV2.model_validate(
            {
                **observation.model_dump(),
                "routing_latency_ms": observation.end_to_end_latency_ms + 1,
            }
        )
    deterministic_with_call = _observation(
        protocol,
        variant_id="deterministic_v1",
        case_id="case_a",
        model_calls=(_model_call(),),
    )
    with pytest.raises(ValueError, match="model-call limit"):
        validate_observation_protocol(deterministic_with_call, protocol)
    wrong_model = _observation(
        protocol,
        variant_id="hybrid_full",
        case_id="case_a",
        model_calls=(_model_call().model_copy(update={"model": "gpt-5.4-nano"}),),
    )
    with pytest.raises(ValueError, match="does not match synthesis binding"):
        validate_observation_protocol(wrong_model, protocol)
    latency_without_protocol_repeats = observation.model_copy(
        update={"phase": EvaluationPhase.LATENCY}
    )
    with pytest.raises(ValueError, match="latency protocol limit"):
        validate_observation_protocol(latency_without_protocol_repeats, protocol)


def test_bundle_recomputes_comparisons_from_observations(tmp_path: Path) -> None:
    protocol = _protocol()
    observations = _paired_observations(protocol)
    comparison = compare_variants(
        observations,
        baseline_variant_id="deterministic_v1",
        candidate_variant_id="hybrid_full",
        metric=ComparisonMetric.TASK_SUCCESS,
        phase=EvaluationPhase.CORRECTNESS,
        bootstrap_samples=200,
    )
    contradictory = comparison.model_copy(update={"median_delta": 999.0})

    with pytest.raises(ValueError, match="does not match bundled observations"):
        write_bundle(
            tmp_path / "contradictory",
            run_id="run_paired_v2",
            protocol=protocol,
            observations=observations,
            comparisons=(contradictory,),
            created_at=datetime(2026, 8, 25, tzinfo=UTC),
        )


def _deterministic_variant() -> EvaluationVariantV2:
    return EvaluationVariantV2(
        variant_id="deterministic_v1",
        description="Deterministic routing, planning, agents, and synthesis.",
        runtime_mode=RuntimeMode.DETERMINISTIC,
        enabled_agents=("product_agent", "review_agent", "trust_agent"),
        embedding_backend="hashing",
    )


def _hybrid_variant() -> EvaluationVariantV2:
    return EvaluationVariantV2(
        variant_id="hybrid_full",
        description="Structured model routing, planning, specialists, and synthesis.",
        runtime_mode=RuntimeMode.HYBRID,
        model_router_enabled=True,
        model_planner_enabled=True,
        model_specialists_enabled=True,
        model_synthesis_enabled=True,
        enabled_agents=("product_agent", "review_agent", "trust_agent"),
        embedding_backend="openai",
        model_bindings=(
            ModelBindingV2(
                stage=ModelStage.ROUTING,
                provider="openai",
                model="gpt-5.4-nano",
            ),
            ModelBindingV2(
                stage=ModelStage.PLANNING,
                provider="openai",
                model="gpt-5.4-nano",
            ),
            ModelBindingV2(
                stage=ModelStage.SPECIALIST,
                provider="openai",
                model="gpt-5.4-nano",
            ),
            ModelBindingV2(
                stage=ModelStage.SYNTHESIS,
                provider="openai",
                model="gpt-5.4-mini",
            ),
        ),
        max_model_calls=8,
        parent_variant_id="deterministic_v1",
        hypothesis="Grounded model reasoning improves task quality at higher cost.",
    )


def _protocol(*, case_order: tuple[str, ...] | None = None) -> EvaluationProtocolV2:
    return EvaluationProtocolV2(
        protocol_id="paired_protocol_v2",
        experiment_sha256="d" * 64,
        baseline_variant_id="deterministic_v1",
        corpus_id="commerce_corpus_v2",
        corpus_sha256="a" * 64,
        dataset_id="synthetic_seed_v2",
        dataset_sha256="b" * 64,
        sample_seed_sha256="e" * 64,
        evaluator_sha256="c" * 64,
        correctness_repeats=1,
        latency_repeats=0,
        bootstrap_samples=1_000,
        case_order=case_order or ("case_a", "case_b", "case_c", "case_d"),
        variants=(_deterministic_variant(), _hybrid_variant()),
        git_revision="abcdef1",
        git_dirty=False,
        network_allowed=True,
    )


def _model_call(*, usage: TokenUsageV2 | None = None) -> ModelCallV2:
    return ModelCallV2(
        call_id="call_synthesis_1",
        stage=ModelStage.SYNTHESIS,
        provider="openai",
        model="gpt-5.4-mini",
        outcome=ModelCallOutcome.SUCCESS,
        latency_ms=25,
        usage=usage,
    )


def _pricing() -> PricingManifestV2:
    return PricingManifestV2(
        pricing_id="openai_pricing_v2",
        provider="openai",
        effective_at=date(2026, 8, 25),
        source_url="https://openai.com/api/pricing/",
        entries=(
            ModelPriceV2(
                model="gpt-5.4-mini",
                input_per_million_usd=Decimal("1.00"),
                cached_input_per_million_usd=Decimal("0.10"),
                output_per_million_usd=Decimal("4.00"),
            ),
        ),
    )


def _observation(
    protocol: EvaluationProtocolV2,
    *,
    variant_id: str,
    case_id: str,
    task_success: bool = True,
    latency_ms: float = 100,
    predicted_intent: str = "product.search",
    model_calls: tuple[ModelCallV2, ...] = (),
    robustness_policy: RobustnessPolicy = RobustnessPolicy.CLEAN,
    parent_case_id: str | None = None,
    transform_id: str | None = None,
    execution_order: int = 0,
    phase: EvaluationPhase = EvaluationPhase.CORRECTNESS,
    estimated_cost_usd: Decimal | None = None,
) -> EvaluationObservationV2:
    return EvaluationObservationV2(
        run_id="run_paired_v2",
        protocol_sha256=protocol_sha256(protocol),
        variant_id=variant_id,
        case_id=case_id,
        category="simple",
        phase=phase,
        repetition=0,
        execution_order=execution_order,
        random_seed=42,
        robustness_policy=robustness_policy,
        parent_case_id=parent_case_id,
        transform_id=transform_id,
        status=TaskStatus.SUCCESS,
        predicted_intent=predicted_intent,
        actions=("product.search",),
        retrieved_product_ids=(1,),
        provenance_source_ids=("postgresql.products",),
        answer="Grounded sample answer.",
        assertions_passed=int(task_success),
        assertion_count=1,
        routing_correct=True,
        exact_plan=True,
        task_success=task_success,
        end_to_end_latency_ms=latency_ms,
        retrieval_f1=1,
        model_calls=model_calls,
        estimated_cost_usd=estimated_cost_usd,
    )


def _paired_observations(
    protocol: EvaluationProtocolV2,
) -> tuple[EvaluationObservationV2, ...]:
    baseline_success = (False, False, True, True)
    candidate_success = (True, True, True, False)
    observations: list[EvaluationObservationV2] = []
    for index, case_id in enumerate(protocol.case_order):
        observations.append(
            _observation(
                protocol,
                variant_id="deterministic_v1",
                case_id=case_id,
                task_success=baseline_success[index],
                latency_ms=100 + index,
                execution_order=index * 2,
            )
        )
        observations.append(
            _observation(
                protocol,
                variant_id="hybrid_full",
                case_id=case_id,
                task_success=candidate_success[index],
                latency_ms=90 + index,
                execution_order=index * 2 + 1,
            )
        )
    return tuple(observations)


def _robustness_protocol() -> EvaluationProtocolV2:
    return _protocol(
        case_order=(
            "clean_a",
            "clean_b",
            "clean_a_typo",
            "clean_b_typo",
            "clean_a_distractor",
            "clean_b_distractor",
        )
    )


def _robustness_observations(
    protocol: EvaluationProtocolV2,
) -> tuple[EvaluationObservationV2, ...]:
    return (
        _observation(protocol, variant_id="hybrid_full", case_id="clean_a"),
        _observation(protocol, variant_id="hybrid_full", case_id="clean_b"),
        _observation(
            protocol,
            variant_id="hybrid_full",
            case_id="clean_a_typo",
            robustness_policy=RobustnessPolicy.LABEL_PRESERVING,
            parent_case_id="clean_a",
            transform_id="typo_noise",
        ),
        _observation(
            protocol,
            variant_id="hybrid_full",
            case_id="clean_b_typo",
            task_success=False,
            robustness_policy=RobustnessPolicy.LABEL_PRESERVING,
            parent_case_id="clean_b",
            transform_id="typo_noise",
        ),
        _observation(
            protocol,
            variant_id="hybrid_full",
            case_id="clean_a_distractor",
            robustness_policy=RobustnessPolicy.LABEL_PRESERVING,
            parent_case_id="clean_a",
            transform_id="irrelevant_distractor",
        ),
        _observation(
            protocol,
            variant_id="hybrid_full",
            case_id="clean_b_distractor",
            predicted_intent="review.summary",
            robustness_policy=RobustnessPolicy.LABEL_PRESERVING,
            parent_case_id="clean_b",
            transform_id="irrelevant_distractor",
        ),
    )
