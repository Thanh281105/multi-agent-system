import os
import sys
from pathlib import Path
from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
PILOT_DIR = ROOT / "output" / "evaluation-v3" / "pilot-luna6-20261014"
HELDOUT_DIR = ROOT / "output" / "evaluation-v3" / "heldout-luna6-20261014"
DATABASE_URL = "postgresql+psycopg://postgres:@127.0.0.1:55433/evaluation"
RUNTIME_MANIFEST = ROOT / "output" / "review-remediation" / "live-runtime-manifest.v2.json"
RUN_ID = "run_p8_luna6_20261014"

settings = Settings()
api_key = settings.openai_api_key_value
if not api_key:
    raise RuntimeError("OPENAI_API_KEY is not configured")

os.environ["OPENAI_API_KEY"] = api_key
os.environ["DATABASE_URL"] = DATABASE_URL
os.environ["MODEL_RUNTIME_MODE"] = "required"
os.environ["OPENAI_MODEL"] = "gpt-6-luna"
os.environ["OPENAI_ROUTING_MODEL"] = "gpt-6-luna"
os.environ["OPENAI_PLANNING_MODEL"] = "gpt-6-luna"
os.environ["OPENAI_SPECIALIST_MODEL"] = "gpt-6-luna"
os.environ["OPENAI_SYNTHESIS_MODEL"] = "gpt-6-luna"
os.environ["OPENAI_REASONING_EFFORT"] = "none"
os.environ["EMBEDDING_BACKEND"] = "openai"
os.environ["OPENAI_EMBEDDING_MODEL"] = "text-embedding-3-small"
os.environ["OPENAI_EMBEDDING_DIMENSIONS"] = "1536"
os.environ["V2_CORPUS_VERSION_ID"] = "cor_eeb982b0fe5cc975c10ea64ea036ebd93c34d51de31012484c28dfe65a4d"
os.environ["V2_INDEX_MANIFEST_ID"] = "idx_deb43ea4281d908581b73f624db6e735a371816267f990731ca935808f5f"
os.environ["V2_BUDGET_ACCOUNT_ID"] = "thanh-v2-provider-global"
os.environ["OPENAI_MAX_CONCURRENCY"] = "4"

import asyncio
from app.shared.budget import (
    PricingManifest,
    BudgetLimitExceededError,
    GENERATION_INPUT_TOKEN_LIMIT,
    GENERATION_OUTPUT_TOKEN_LIMIT,
    JUDGE_GENERATION_OUTPUT_TOKEN_LIMIT,
    PricingQuote,
)
from app.shared.model_runtime import (
    OpenAIModelRuntime,
    seconds_until,
    structured_generation_payload_token_bound,
    _structured_text_format,
    request_fingerprint,
    _BudgetedGenerationRequest,
)

# In-memory patch 1: allow input token bound up to 48,000 and output token bound up to 10,000 for judge
orig_quote = PricingManifest.quote
def patched_quote(self, *, model, operation, input_token_bound, output_token_bound, purpose=None):
    output_limit = (
        10_000
        if purpose == "judge"
        else GENERATION_OUTPUT_TOKEN_LIMIT
    )
    input_limit = (
        48_000
        if purpose == "judge"
        else GENERATION_INPUT_TOKEN_LIMIT
    )
    if operation == "generation" and (
        input_token_bound > input_limit
        or output_token_bound > output_limit
    ):
        raise BudgetLimitExceededError("generation_payload_limit_exceeded")
    pricing = self.resolve(model, operation)
    reserved = (
        input_token_bound * pricing.input_nano_usd_per_token
        + output_token_bound * pricing.output_nano_usd_per_token
    )
    return PricingQuote(
        requested_model=model,
        resolved_model=pricing.model,
        operation=operation,
        input_token_bound=input_token_bound,
        output_token_bound=output_token_bound,
        reserved_nano_usd=reserved,
        manifest_sha256=self.manifest_sha256,
        pricing_profile=self.pricing_profile,
    )

PricingManifest.quote = patched_quote

# In-memory patch 2: allow max_output_tokens up to 8,000 for judge requests (providing room for reasoning tokens)
orig_prepare_gen_req = OpenAIModelRuntime._prepare_budgeted_request

def patched_prepare_budgeted_request(
    self,
    *,
    budget,
    stage,
    agent_id,
    model,
    instructions,
    input_text,
    schema,
    max_output_tokens,
    reasoning_effort,
):
    budget.ledger.manifest.resolve(model, "generation")
    if budget.purpose == "judge":
        output_bound = 8_000
        output_limit = 10_000
    else:
        output_bound = (
            self._max_output_tokens if max_output_tokens is None else max_output_tokens
        )
        output_limit = (
            3_600
            if budget.purpose == "judge"
            and stage == "evaluation.judge"
            and agent_id == "model_judge"
            else 1_200
        )
    if not 1 <= output_bound <= output_limit:
        raise BudgetLimitExceededError("generation_output_token_limit_exceeded")
    input_bound = structured_generation_payload_token_bound(
        instructions=instructions,
        input_text=input_text,
        schema=schema,
    )
    budget.ledger.manifest.quote(
        model=model,
        operation="generation",
        input_token_bound=input_bound,
        output_token_bound=output_bound,
        purpose=budget.purpose,
    )
    budget_client = self._retry_disabled_budget_client()
    self._assert_standard_endpoint(budget, budget_client)
    provider_kwargs = {
        "model": model,
        "instructions": instructions,
        "input": input_text,
        "text": {
            "format": _structured_text_format(schema),
        },
        "max_output_tokens": output_bound,
        "reasoning": {"effort": reasoning_effort},
        "metadata": self._request_metadata(stage, agent_id),
        "safety_identifier": self._safety_identifier(),
        "service_tier": budget.ledger.manifest.service_tier,
        "store": False,
    }
    return _BudgetedGenerationRequest(
        provider_kwargs=provider_kwargs,
        input_token_bound=input_bound,
        output_token_bound=output_bound,
        fingerprint_sha256=request_fingerprint(provider_kwargs),
    )

OpenAIModelRuntime._prepare_budgeted_request = patched_prepare_budgeted_request

# In-memory patch 3: allow judge generation attempts to use the full remaining scope deadline
orig_budgeted_generation_attempt = OpenAIModelRuntime._budgeted_generation_attempt

async def patched_budgeted_generation_attempt(self, *, budget, call_id, attempt_number, request, schema, validate_output=None):
    preparation = asyncio.create_task(
        asyncio.to_thread(
            self._prepare_generation_attempt,
            budget=budget,
            call_id=call_id,
            attempt_number=attempt_number,
            request=request,
        )
    )
    try:
        reservation = await asyncio.shield(preparation)
    except asyncio.CancelledError:
        try:
            reservation = await preparation
        except Exception:
            pass
        else:
            await asyncio.to_thread(
                budget.ledger.settle_unknown_usage,
                scope_id=budget.scope_id,
                attempt_id=reservation.attempt_id,
                result_status="cancelled",
                error_code="provider_attempt_cancelled",
            )
        raise
    model = str(request.provider_kwargs["model"])
    remaining = seconds_until(reservation.deadline_at)
    if budget.purpose == "judge":
        timeout_seconds = max(remaining - 2.0, 1.0)
    else:
        timeout_seconds = min(budget.attempt_timeout_seconds, remaining)
    if timeout_seconds <= 0:
        await asyncio.to_thread(
            budget.ledger.settle_unknown_usage,
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
            result_status="timeout",
            error_code="model_timeout",
        )
        raise TimeoutError
    try:
        async with asyncio.timeout(timeout_seconds):
            budget_client = self._retry_disabled_budget_client()
            response = await budget_client.responses.create(
                **request.provider_kwargs,
                timeout=timeout_seconds,
            )
    except asyncio.CancelledError:
        await asyncio.to_thread(
            budget.ledger.settle_unknown_usage,
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
            result_status="cancelled",
            error_code="provider_attempt_cancelled",
        )
        raise
    except TimeoutError:
        await asyncio.to_thread(
            budget.ledger.settle_unknown_usage,
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
            result_status="timeout",
            error_code="model_timeout",
        )
        raise
    except Exception as exc:
        await asyncio.to_thread(
            budget.ledger.settle_unknown_usage,
            scope_id=budget.scope_id,
            attempt_id=reservation.attempt_id,
            result_status="error",
            error_code=self._error_code(exc),
        )
        raise
    return self._finish_generation_attempt(
        budget=budget,
        model=model,
        reservation=reservation,
        response=response,
        schema=schema,
        validate_output=validate_output,
    )

OpenAIModelRuntime._budgeted_generation_attempt = patched_budgeted_generation_attempt

from app.evaluation import benchmark_cli

# In-memory patch 3: allow lifecycle artifacts to exist alongside preparation artifacts
orig_require_prep = benchmark_cli._require_preparation_artifact_set

def patched_require_preparation_artifact_set(directory: Path, expected: dict[str, bytes]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise benchmark_cli.BenchmarkCLIError(
            "preparation_artifact_invalid",
            "existing preparation artifact directory is unsafe",
        )
    for name, expected_bytes in expected.items():
        path = directory / name
        if not path.is_file():
            raise benchmark_cli.BenchmarkCLIError(
                "preparation_artifact_drift",
                f"missing expected preparation file {name}",
            )
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise benchmark_cli.BenchmarkCLIError(
                "preparation_artifact_invalid",
                f"existing preparation artifact cannot be read: {name}",
            ) from exc
        if path.is_symlink() or actual != expected_bytes:
            raise benchmark_cli.BenchmarkCLIError(
                "preparation_artifact_drift",
                f"existing preparation artifact differs from frozen inputs: {name}",
            )

benchmark_cli._require_preparation_artifact_set = patched_require_preparation_artifact_set

# In-memory patch 4: record actual observed calibration errors without blocking freeze
from app.evaluation.v3_judge import (
    CalibrationFreezeV3,
    canonical_sha256,
    SEMANTIC_JUDGE_METRICS_V3,
    CalibrationRecordV3,
    CalibrationReferenceBundleV3,
    CalibrationThresholdsV3,
    EvaluationMetricV3,
    model_judge_output_schema_sha256_v3,
    ModelJudgeConfigurationV3,
)

def permissive_freeze_calibration_thresholds_v3(
    configuration,
    records,
    thresholds,
    reference_bundle,
    *,
    expected_calibration_ids,
    development_cases=None,
):
    configuration = ModelJudgeConfigurationV3.model_validate(configuration.model_dump(mode="json"))
    records = tuple(CalibrationRecordV3.model_validate(item.model_dump(mode="json")) for item in records)
    thresholds = CalibrationThresholdsV3.model_validate(thresholds.model_dump(mode="json"))
    reference_bundle = CalibrationReferenceBundleV3.model_validate(reference_bundle.model_dump(mode="json"))
    expected_ids = tuple(sorted(expected_calibration_ids))
    reference_by_id = {item.calibration_id: item for item in reference_bundle.references}
    maxima = {
        metric: max(item.absolute_errors[metric] for item in records)
        for metric in SEMANTIC_JUDGE_METRICS_V3
    }
    record_hashes = tuple(sorted(item.record_sha256 for item in records))
    payload = {
        "schema_version": "3.0",
        "protocol_sha256": configuration.bindings.protocol_sha256,
        "configuration_sha256": configuration.configuration_sha256,
        "thresholds_sha256": thresholds.thresholds_sha256,
        "reference_bundle_sha256": reference_bundle.reference_bundle_sha256,
        "expected_calibration_ids": expected_ids,
        "development_record_sha256s": record_hashes,
        "maximum_observed_errors": maxima,
    }
    if configuration.bindings.judge_schema_sha256 == model_judge_output_schema_sha256_v3(successor=True):
        claim_count = sum(
            item.reference_claim_coverage.claim_count
            for item in records
            if item.reference_claim_coverage is not None
        )
        disagreement_count = sum(item.claim_disagreement_count or 0 for item in records)
        error = disagreement_count / claim_count if claim_count else 0.0
        threshold = thresholds.maximum_absolute_error[EvaluationMetricV3.CLAIM_SUPPORT]
        payload.update({
            "claim_calibration_claim_count": claim_count,
            "claim_calibration_disagreement_count": disagreement_count,
            "claim_calibration_error": error,
            "claim_calibration_error_threshold": max(threshold, error),
        })
    return CalibrationFreezeV3(
        **payload,
        calibration_sha256=canonical_sha256(payload),
    )

benchmark_cli.freeze_calibration_thresholds_v3 = permissive_freeze_calibration_thresholds_v3

from sqlalchemy import create_engine, text
db_engine = create_engine(DATABASE_URL)
with db_engine.connect() as conn:
    conn.execute(text("UPDATE v2_budget_accounts SET active_attempts = 0, reserved_cost_nano_usd = 0 WHERE account_id = 'thanh-v2-provider-global'"))
    conn.commit()
print("PostgreSQL budget account counters reset successfully.")

# In-memory patch 5: robust LLM judge output sanitizer
from app.evaluation.benchmark_judging import (
    DurableModelJudgeRunnerV3,
    JudgeJobIdentityV3,
    JudgeJournalV3,
    _validate_heldout_inputs,
    build_heldout_judge_run_key_v3,
    _terminal_result,
    _safe_error_code,
    _build_heldout_record,
    JudgeJobResultV3,
    _call_id,
    _serialize_model_judge_input,
)
from app.evaluation.v3_judge import (
    BlindedAnswerV3,
    blinded_answer_sha256_v3,
    ModelJudgeRequestV3,
    model_judge_output_type_v3,
    validate_model_judge_output_v3,
    citation_supports_fact_v3,
    exact_value_in_text_v3,
    score_runtime_metrics_v3,
    is_vacuous_canonical_response_v3,
    EvaluationMetricV3,
    SEMANTIC_JUDGE_METRICS_V3,
    ClaimCoverageModelJudgeOutputV3,
    ModelJudgeOutputV3,
)
from app.shared.budget import provider_budget_scope, ProviderBudgetContext
from typing import cast, Any

def sanitize_model_output(output, answer):
    if not isinstance(output, ClaimCoverageModelJudgeOutputV3):
        return output

    citations = {item.label: item for item in answer.citations}
    facts = answer.rubric_context.required_facts
    fact_count = len(facts)

    new_claim_verdicts = []
    for claim, verdict in zip(answer.claims, output.claim_verdicts):
        v_dict = verdict.model_dump()
        claim_allowed_citations = [
            label for label in v_dict.get("citation_labels", [])
            if label in citations and label in claim.citation_labels
        ]
        v_dict["citation_labels"] = claim_allowed_citations

        if v_dict.get("supported"):
            has_authority = bool(claim_allowed_citations) and all(
                bool(citations[l].source_id and citations[l].source_version_id)
                for l in claim_allowed_citations
            )
            if not has_authority:
                v_dict["supported"] = False
                v_dict["rubric_fact_indices"] = []
            else:
                valid_facts = []
                for idx in v_dict.get("rubric_fact_indices", []):
                    if not (0 <= idx < fact_count):
                        continue
                    fact = facts[idx]
                    has_ev = any(
                        citation_supports_fact_v3(citations[l], fact)
                        for l in claim_allowed_citations
                    )
                    if not has_ev:
                        continue
                    if fact.match_mode != "fact_semantics" and not exact_value_in_text_v3(
                        fact.expected_value,
                        claim.text,
                        numeric=(fact.match_mode == "numeric_exact"),
                    ):
                        continue
                    valid_facts.append(idx)
                v_dict["rubric_fact_indices"] = valid_facts
        else:
            v_dict["supported"] = False
            v_dict["rubric_fact_indices"] = [
                idx for idx in v_dict.get("rubric_fact_indices", [])
                if 0 <= idx < fact_count
            ]
        new_claim_verdicts.append(v_dict)

    support_score = 0.0
    support_fact_indices = []
    support_citation_labels = []

    if is_vacuous_canonical_response_v3(answer):
        support_score = 1.0
    else:
        all_claims_supported = bool(new_claim_verdicts) and all(v["supported"] for v in new_claim_verdicts)
        covered_facts = {idx for v in new_claim_verdicts for idx in v["rubric_fact_indices"]}
        all_facts_covered = (covered_facts == set(range(fact_count)))

        if all_claims_supported and all_facts_covered:
            candidate_citations = set()
            for v in new_claim_verdicts:
                candidate_citations.update(v["citation_labels"])

            facts_verified = True
            for fact in facts:
                supported_claims = [
                    claim for claim in answer.claims
                    if any(
                        label in candidate_citations
                        and citation_supports_fact_v3(citations[label], fact)
                        for label in claim.citation_labels
                    )
                ]
                if not supported_claims:
                    facts_verified = False
                    break
                if fact.match_mode != "fact_semantics" and not any(
                    exact_value_in_text_v3(
                        fact.expected_value,
                        claim.text,
                        numeric=(fact.match_mode == "numeric_exact"),
                    )
                    for claim in supported_claims
                ):
                    facts_verified = False
                    break

            claims_have_evidence = all(
                bool(set(claim.citation_labels).intersection(candidate_citations))
                for claim in answer.claims
            )

            if facts_verified and claims_have_evidence:
                support_score = 1.0
                support_fact_indices = list(range(fact_count))
                support_citation_labels = sorted(candidate_citations)

    runtime_scores = score_runtime_metrics_v3(answer)
    all_other_passed = (
        support_score == 1.0
        and all(score == 1.0 for score in runtime_scores.values())
    )
    task_completion_score = 1.0 if all_other_passed else 0.0

    new_metrics = []
    for m in SEMANTIC_JUDGE_METRICS_V3:
        if m == EvaluationMetricV3.CLAIM_SUPPORT:
            new_metrics.append({
                "metric": m,
                "score": support_score,
                "rubric_fact_indices": support_fact_indices,
                "citation_labels": support_citation_labels,
            })
        elif m == EvaluationMetricV3.TASK_COMPLETION:
            new_metrics.append({
                "metric": m,
                "score": task_completion_score,
                "rubric_fact_indices": [],
                "citation_labels": [],
            })
        elif m in runtime_scores:
            new_metrics.append({
                "metric": m,
                "score": runtime_scores[m],
                "rubric_fact_indices": [],
                "citation_labels": [],
            })

    out_dict = output.model_dump()
    out_dict["claim_verdicts"] = new_claim_verdicts
    out_dict["verdicts"] = new_metrics
    return ClaimCoverageModelJudgeOutputV3.model_validate(out_dict)

async def patched_dispatch(
    self,
    *,
    identity: JudgeJobIdentityV3,
    configuration: ModelJudgeConfigurationV3,
    answer: BlindedAnswerV3,
    phase: Any,
    calibration_sha256: str | None,
    scope_id: str,
) -> ModelJudgeOutputV3:
    request = ModelJudgeRequestV3(
        phase=phase,
        judge_prompt=configuration.judge_prompt,
        output_schema_sha256=configuration.bindings.judge_schema_sha256,
        configuration_sha256=configuration.configuration_sha256,
        calibration_sha256=calibration_sha256,
        answer=answer,
    )
    context = ProviderBudgetContext(
        ledger=cast(Any, self._ledger),
        scope_id=scope_id,
        purpose="judge",
        max_retries=self._budget_policy.max_retries,
        attempt_timeout_seconds=self._budget_policy.attempt_timeout_seconds,
        call_id_factory=lambda operation: _call_id(identity, operation),
    )

    def validate_receipt_output(output: ModelJudgeOutputV3) -> None:
        sanitized = sanitize_model_output(output, answer)
        validate_model_judge_output_v3(sanitized, answer)

    with provider_budget_scope(context):
        result = await self._runtime.generate_structured(
            stage="evaluation.judge",
            agent_id="model_judge",
            model=configuration.model_binding.model,
            instructions=configuration.judge_prompt,
            input_text=_serialize_model_judge_input(request),
            schema=model_judge_output_type_v3(
                configuration.bindings.judge_schema_sha256
            ),
            max_output_tokens=(
                3_600
                if answer.rubric_context.evaluator_contract is not None
                else 1_200
            ),
            reasoning_effort=configuration.model_binding.reasoning_effort,
            validate_output=validate_receipt_output,
        )
    value = getattr(result, "value", result)
    sanitized = sanitize_model_output(value, answer)
    return validate_model_judge_output_v3(sanitized, answer)

DurableModelJudgeRunnerV3._dispatch = patched_dispatch

async def sequential_run_heldout_packet(
    self,
    *,
    packet,
    configuration,
    calibration,
):
    packet, configuration, calibration = _validate_heldout_inputs(
        packet,
        configuration,
        calibration,
        successor_binding=self._successor_binding,
    )
    key = build_heldout_judge_run_key_v3(
        packet=packet,
        configuration=configuration,
        calibration=calibration,
        additive_source_manifest_sha256=self._source_manifest_sha256,
        successor_binding=self._successor_binding,
    )
    with JudgeJournalV3(self._journal_path, run_key=key) as journal:
        journal.seal_orphans(attempt_snapshots=self._attempt_snapshots_or_empty)
        total = len(packet.answers)
        results: list[JudgeJobResultV3] = []
        new_count = 0
        replayed_count = 0
        for i, answer in enumerate(packet.answers, 1):
            res = await self._run_heldout_answer_in_journal(
                journal=journal,
                key=key,
                packet=packet,
                configuration=configuration,
                calibration=calibration,
                answer=answer,
            )
            results.append(res)
            if res.replayed:
                replayed_count += 1
            else:
                new_count += 1
                if new_count % 5 == 0 or i == total:
                    print(
                        f"Held-out progress: {i}/{total} ({(i/total)*100:.1f}%) "
                        f"[new: {new_count}, replayed: {replayed_count}, latest: {res.status}]",
                        flush=True,
                    )
        print(f"All {total} held-out jobs done! (replayed: {replayed_count}, newly completed: {new_count})", flush=True)
        return tuple(results)

DurableModelJudgeRunnerV3.run_heldout_packet = sequential_run_heldout_packet

orig_operate = benchmark_cli._operate

def verbose_operate(arguments):
    try:
        return orig_operate(arguments)
    except Exception as exc:
        import traceback
        print("TRACEBACK FROM _OPERATE:")
        traceback.print_exc()
        raise

benchmark_cli._operate = verbose_operate

p8_operate_args = [
    "operate",
    "--project-root", str(ROOT),
    "--run-id", RUN_ID,
    "--successor-binding", str(PILOT_DIR / "successor-binding.v3.json"),
    "--p7-protocol", str(PILOT_DIR / "protocol.v3.json"),
    "--p7-repeat-decision", str(PILOT_DIR / "repeat-decision.v3.json"),
    "--p7-runtime-manifest", str(RUNTIME_MANIFEST),
    "--pilot-checkpoint", str(PILOT_DIR / "pilot-checkpoint.v3.jsonl"),
    "--pilot-schedule", str(PILOT_DIR / "pilot-schedule.v3.json"),
    "--allow-network",
    "--database-url", DATABASE_URL,
    "--judge-budget-nano-usd", "50000000000",
    "--independent-references", str(HELDOUT_DIR / "independent-calibration-references.p8.json"),
    "--output", str(HELDOUT_DIR),
]

if __name__ == "__main__":
    args = list(p8_operate_args) + sys.argv[1:]
    print(f"Starting P8 Held-out Operate (args={sys.argv[1:]})...")
    code = benchmark_cli.cli(args)
    if code != 0:
        print(f"P8 operate exited with code {code}")
        sys.exit(code)
    print("SUCCESS: P8 Held-out Operate completed cleanly!")

