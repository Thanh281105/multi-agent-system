"""Additive Package 8 driver for the frozen Package 7 held-out benchmark.

Package 7's ``v3_*`` modules deliberately model only the pilot.  This module
keeps that protocol byte-compatible and adds the held-out matrix as a separate
descriptor: sixty held-out conversations, the frozen four variants, and the
single global repeat decision.  It reuses the V3 canonical identities,
executor evidence contracts, and append-only checkpoint state machine without
changing the pilot implementation.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evaluation.protocol import canonical_json_bytes, canonical_sha256
from app.evaluation.v3_checkpoint import CheckpointWriterV3
from app.evaluation.v3_gold import (
    EvaluationSplitV3,
    LoadedEvaluationGoldV3,
    load_evaluation_gold_v3,
)
from app.evaluation.v3_models import (
    PACKAGE7_VARIANT_ORDER,
    EvaluationProtocolV3,
    ObservationIdentityV3,
    RepeatDecisionV3,
    ScheduledTurnKindV3,
    ScheduledTurnV3,
    canonical_observation_id_v3,
    canonical_turn_id_v3,
)
from app.evaluation.v3_protocol import (
    evaluation_protocol_sha256_v3,
    validate_evaluation_protocol_v3,
)
from app.evaluation.v3_runner import (
    AmbiguousObservationReceiptV3,
    EvaluationCaseV3,
    EvaluationResourceLimitsV3,
    EvaluationRunResultV3,
    EvaluationUserTurnV3,
    InitialStateResetReceiptV3,
    ObservationExecutionFailureV3,
    ObservationExecutorFactoryV3,
    ObservationResourceLimitErrorV3,
    ObservationRunReceiptV3,
    ObservationTerminalStatusV3,
    SandboxFixtureAdapterV3,
    UserTurnExecutionResultV3,
    _build_receipt,
    _enforce_resource_limits,
    _execution_context,
    _load_terminal_receipts,
    _merge_partial_results,
    _run_result,
    _turn_request,
    _validate_observation_results,
    _validate_turn_result,
    execution_case_set_sha256_v3,
    execution_case_sha256_v3,
    initial_state_reset_receipt_v3,
)
from app.evaluation.v3_schedule import (
    SCHEDULE_ALGORITHM_ID_V3,
    pilot_schedule_sha256_v3,
)

EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3 = (
    "315efff0d596f11ea63aa60729834e54fb5d6acb9bf6b7d2b9260b96b601451f"
)
PACKAGE8_HELDOUT_EXTENSION_ID_V3 = "package8_heldout_driver_v1"
PACKAGE8_HELDOUT_SCHEDULE_ID_V3 = "package8_heldout_interleaved_v1"
_MEASUREMENT_SEED_MASK = 0xE7A13
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")

_PACKAGE8_HELDOUT_DESCRIPTOR = {
    "extension_id": PACKAGE8_HELDOUT_EXTENSION_ID_V3,
    "schedule_id": PACKAGE8_HELDOUT_SCHEDULE_ID_V3,
    "identity_schedule_algorithm_id": SCHEDULE_ALGORITHM_ID_V3,
    "identity_compatibility": "canonical_package7_v3_identity_contract",
    "case_source": "frozen_gold_split_entries_filtered_to_held_out_in_roster_order",
    "measurement_ordering": "case_then_repeat_then_seeded_variant_shuffle",
    "measurement_seed_mask": _MEASUREMENT_SEED_MASK,
    "warmups": 0,
    "variants": 4,
    "heldout_conversations": 60,
    "repeat_policy": "one_frozen_global_repeat_count_for_all_variants",
    "source_hash": "sha256_lf_normalized_additive_source_manifest",
    "source_files": [
        "app/evaluation/benchmark_cli.py",
        "app/evaluation/benchmark_v3.py",
    ],
}
_PACKAGE8_ADDITIVE_SOURCE_NAMES = ("benchmark_cli.py", "benchmark_v3.py")


class Package8BenchmarkError(RuntimeError):
    """Fail-closed error for an invalid held-out benchmark binding."""


class FrozenPackage7DriftError(Package8BenchmarkError):
    """The supplied Package 7 artifacts or current source bindings changed."""


class FrozenContractP8(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class HeldoutExecutionPlanV3(FrozenContractP8):
    """The P8 manifest that binds every held-out execution input."""

    schema_version: Literal["3.0"] = "3.0"
    extension_id: Literal["package8_heldout_driver_v1"] = "package8_heldout_driver_v1"
    extension_descriptor_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    additive_source_files: tuple[
        Literal["app/evaluation/benchmark_cli.py"],
        Literal["app/evaluation/benchmark_v3.py"],
    ] = (
        "app/evaluation/benchmark_cli.py",
        "app/evaluation/benchmark_v3.py",
    )
    additive_source_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    package7_protocol_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    repeat_decision_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    heldout_schedule_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    selected_repeats: Literal[2, 3]
    heldout_case_count: Literal[60] = 60
    variant_count: Literal[4] = 4
    measured_cell_count: Literal[480, 720] = 720
    warmup_count: Literal[0] = 0
    identity_schedule_algorithm_id: Literal["package7_pilot_interleaved_v1"] = (
        SCHEDULE_ALGORITHM_ID_V3
    )

    @model_validator(mode="after")
    def validate_measured_cell_count(self) -> HeldoutExecutionPlanV3:
        expected = self.heldout_case_count * self.variant_count * self.selected_repeats
        if self.measured_cell_count != expected:
            raise ValueError(
                "measured cell count must match the frozen repeat decision"
            )
        return self


@dataclass(frozen=True, slots=True)
class FrozenPackage7HeldoutInputsV3:
    """Validated frozen Package 7 inputs plus exact P8 case adapters."""

    project_root: Path
    protocol: EvaluationProtocolV3
    protocol_sha256: str
    repeat_decision: RepeatDecisionV3
    repeat_decision_sha256: str
    loaded_gold: LoadedEvaluationGoldV3
    heldout_cases: Mapping[str, EvaluationCaseV3]


def package8_heldout_descriptor_sha256_v3() -> str:
    """Return the stable descriptor hash for the additive P8 extension."""

    return canonical_sha256(_PACKAGE8_HELDOUT_DESCRIPTOR)


def package8_additive_source_sha256_v3() -> str:
    """Hash both P8 sources as one LF-normalized additive source manifest."""

    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in _PACKAGE8_ADDITIVE_SOURCE_NAMES:
        path = root / name
        if not path.is_file():
            raise Package8BenchmarkError("package8_additive_source_missing")
        normalized = path.read_bytes().replace(b"\r\n", b"\n")
        content_sha256 = hashlib.sha256(normalized).hexdigest()
        digest.update(f"app/evaluation/{name}\0{content_sha256}\n".encode("utf-8"))
    return digest.hexdigest()


def load_frozen_package7_heldout_inputs_v3(
    *,
    project_root: Path,
    protocol_path: Path,
    repeat_decision_path: Path,
    gold_path: Path,
    split_path: Path,
) -> FrozenPackage7HeldoutInputsV3:
    """Load P7 artifacts, reject drift, and adapt all and only held-out cases."""

    root = project_root.resolve()
    protocol = _load_model(protocol_path, EvaluationProtocolV3, "protocol")
    validate_evaluation_protocol_v3(protocol)
    protocol_sha256 = evaluation_protocol_sha256_v3(protocol)
    if protocol_sha256 != EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3:
        raise FrozenPackage7DriftError("package7_protocol_hash_drift")

    decision = _load_model(repeat_decision_path, RepeatDecisionV3, "repeat_decision")
    decision_sha256 = canonical_sha256(decision)
    if (
        decision.protocol_sha256 != protocol_sha256
        or decision.repeat_rule_sha256 != protocol.repeat_rule_sha256
    ):
        raise FrozenPackage7DriftError("package7_repeat_decision_binding_drift")
    _validated_repeat_count_v3(decision)

    loaded_gold = load_evaluation_gold_v3(
        gold_path,
        split_path,
        project_root=root,
    )
    if (
        protocol.assets.gold_sha256 != loaded_gold.gold_sha256
        or protocol.assets.split_sha256 != loaded_gold.split_sha256
    ):
        raise FrozenPackage7DriftError("package7_gold_split_binding_drift")
    _validate_live_package7_binding(root, protocol)
    heldout_cases = build_heldout_cases_v3(loaded_gold)
    return FrozenPackage7HeldoutInputsV3(
        project_root=root,
        protocol=protocol,
        protocol_sha256=protocol_sha256,
        repeat_decision=decision,
        repeat_decision_sha256=decision_sha256,
        loaded_gold=loaded_gold,
        heldout_cases=heldout_cases,
    )


def build_heldout_cases_v3(
    loaded: LoadedEvaluationGoldV3,
) -> dict[str, EvaluationCaseV3]:
    """Adapt the exact held-out roster while retaining work groups and fixtures."""

    conversations = {item.conversation_id: item for item in loaded.gold.conversations}
    cases: dict[str, EvaluationCaseV3] = {}
    heldout_entries = tuple(
        entry
        for entry in loaded.split.entries
        if entry.split is EvaluationSplitV3.HELD_OUT
    )
    if len(heldout_entries) != 60:
        raise FrozenPackage7DriftError("heldout_roster_count_drift")

    for entry in heldout_entries:
        gold_case = conversations.get(entry.conversation_id)
        if gold_case is None or gold_case.split is not EvaluationSplitV3.HELD_OUT:
            raise FrozenPackage7DriftError("heldout_gold_binding_drift")
        fixture_matches = gold_case.sandbox_fixture is None and (
            entry.sandbox_fixture_id is None and entry.sandbox_fixture_sha256 is None
        )
        if gold_case.sandbox_fixture is not None:
            fixture_matches = (
                gold_case.sandbox_fixture.fixture_id == entry.sandbox_fixture_id
                and canonical_sha256(gold_case.sandbox_fixture.model_dump(mode="json"))
                == entry.sandbox_fixture_sha256
            )
        if (
            gold_case.work_group_id != entry.work_group_id
            or gold_case.category != entry.category
            or not fixture_matches
        ):
            raise FrozenPackage7DriftError("heldout_case_binding_drift")
        fixture_payload = (
            gold_case.sandbox_fixture.model_dump(mode="json")
            if gold_case.sandbox_fixture is not None
            else None
        )
        fixture = (
            SandboxFixtureAdapterV3(
                fixture_id=gold_case.sandbox_fixture.fixture_id,
                fixture_sha256=canonical_sha256(fixture_payload),
                reset_revision=gold_case.sandbox_fixture.reset_revision,
                payload=fixture_payload,
            )
            if fixture_payload is not None and gold_case.sandbox_fixture is not None
            else None
        )
        cases[entry.conversation_id] = EvaluationCaseV3(
            case_id=entry.conversation_id,
            work_group_id=entry.work_group_id,
            category=gold_case.category.value,
            principal_role=gold_case.identity_fixture.role.value,
            scopes=tuple(scope.value for scope in gold_case.identity_fixture.scopes),
            resolved_product_ids=gold_case.product_ids,
            user_turns=tuple(
                EvaluationUserTurnV3(
                    source_turn_id=turn.turn_id,
                    ordinal=turn.ordinal,
                    message=turn.message,
                )
                for turn in gold_case.user_turns
            ),
            initial_state={
                "identity_fixture": gold_case.identity_fixture.model_dump(mode="json")
            },
            sandbox_fixture=fixture,
        )
    if tuple(cases) != tuple(entry.conversation_id for entry in heldout_entries):
        raise FrozenPackage7DriftError("heldout_case_order_drift")
    return cases


def build_heldout_schedule_v3(
    frozen: FrozenPackage7HeldoutInputsV3,
    *,
    run_id: str,
) -> tuple[ScheduledTurnV3, ...]:
    """Build the exact 60 x 4 x frozen-repeat P8 matrix without warmups."""

    if _IDENTIFIER.fullmatch(run_id) is None:
        raise ValueError("run_id is not a valid evaluation identifier")
    selected_repeats = _validated_repeat_count_v3(frozen.repeat_decision)
    variants = tuple(variant.variant_id for variant in frozen.protocol.variants)
    if variants != PACKAGE7_VARIANT_ORDER:
        raise FrozenPackage7DriftError("package7_variant_order_drift")
    case_ids = _heldout_case_order(frozen.loaded_gold)
    if tuple(frozen.heldout_cases) != case_ids or len(case_ids) != 60:
        raise FrozenPackage7DriftError("heldout_roster_count_drift")

    generator = random.Random(frozen.protocol.random_seed ^ _MEASUREMENT_SEED_MASK)
    turns: list[ScheduledTurnV3] = []
    execution_order = 0
    for case_id in case_ids:
        case = frozen.heldout_cases[case_id]
        for repetition in range(selected_repeats):
            shuffled_variants = list(variants)
            generator.shuffle(shuffled_variants)
            for variant_id in shuffled_variants:
                identity = ObservationIdentityV3(
                    run_id=run_id,
                    protocol_sha256=frozen.protocol_sha256,
                    schedule_algorithm_id=SCHEDULE_ALGORITHM_ID_V3,
                    turn_kind=ScheduledTurnKindV3.MEASURED,
                    variant_id=variant_id,
                    case_id=case_id,
                    work_group_id=case.work_group_id,
                    repetition=repetition,
                )
                turns.append(
                    ScheduledTurnV3(
                        turn_id=canonical_turn_id_v3(identity),
                        observation_id=canonical_observation_id_v3(identity),
                        identity=identity,
                        schedule_index=len(turns),
                        execution_order=execution_order,
                    )
                )
                execution_order += 1
    schedule = tuple(turns)
    validate_heldout_schedule_v3(frozen, schedule, run_id=run_id)
    return schedule


def validate_heldout_schedule_v3(
    frozen: FrozenPackage7HeldoutInputsV3,
    schedule: Sequence[ScheduledTurnV3],
    *,
    run_id: str,
) -> None:
    """Prove that a schedule is the complete frozen P8 measurement matrix."""

    turns = tuple(schedule)
    selected_repeats = _validated_repeat_count_v3(frozen.repeat_decision)
    expected_case_ids = _heldout_case_order(frozen.loaded_gold)
    if tuple(frozen.heldout_cases) != expected_case_ids:
        raise FrozenPackage7DriftError("heldout_case_order_drift")
    expected = {
        (case_id, variant_id, repetition)
        for case_id in expected_case_ids
        for variant_id in PACKAGE7_VARIANT_ORDER
        for repetition in range(selected_repeats)
    }
    actual = {
        (turn.identity.case_id, turn.identity.variant_id, turn.identity.repetition)
        for turn in turns
    }
    measured_cell_count = 60 * len(PACKAGE7_VARIANT_ORDER) * selected_repeats
    if (
        len(turns) != measured_cell_count
        or actual != expected
        or len(actual) != len(turns)
    ):
        raise FrozenPackage7DriftError("heldout_schedule_matrix_drift")
    if [turn.schedule_index for turn in turns] != list(range(measured_cell_count)):
        raise FrozenPackage7DriftError("heldout_schedule_index_drift")
    if [turn.execution_order for turn in turns] != list(range(measured_cell_count)):
        raise FrozenPackage7DriftError("heldout_execution_order_drift")
    for turn in turns:
        identity = turn.identity
        if (
            identity.run_id != run_id
            or identity.protocol_sha256 != frozen.protocol_sha256
            or identity.schedule_algorithm_id != SCHEDULE_ALGORITHM_ID_V3
            or identity.turn_kind is not ScheduledTurnKindV3.MEASURED
            or identity.work_group_id
            != frozen.heldout_cases[identity.case_id].work_group_id
        ):
            raise FrozenPackage7DriftError("heldout_schedule_binding_drift")


def build_heldout_execution_plan_v3(
    frozen: FrozenPackage7HeldoutInputsV3,
    schedule: Sequence[ScheduledTurnV3],
    *,
    run_id: str,
) -> HeldoutExecutionPlanV3:
    """Bind P7 provenance and the full P8 schedule into one immutable plan."""

    validate_heldout_schedule_v3(frozen, schedule, run_id=run_id)
    selected_repeats = _validated_repeat_count_v3(frozen.repeat_decision)
    measured_cell_count: Literal[480, 720] = 480 if selected_repeats == 2 else 720
    return HeldoutExecutionPlanV3(
        extension_descriptor_sha256=package8_heldout_descriptor_sha256_v3(),
        additive_source_manifest_sha256=package8_additive_source_sha256_v3(),
        package7_protocol_sha256=frozen.protocol_sha256,
        repeat_decision_sha256=frozen.repeat_decision_sha256,
        heldout_schedule_sha256=pilot_schedule_sha256_v3(tuple(schedule)),
        selected_repeats=selected_repeats,
        measured_cell_count=measured_cell_count,
    )


def validate_heldout_execution_plan_v3(
    plan: HeldoutExecutionPlanV3,
    expected: HeldoutExecutionPlanV3,
) -> None:
    """Reject a stale P8 plan before checkpoint or executor work starts."""

    if plan != expected:
        raise FrozenPackage7DriftError("package8_execution_plan_drift")


class HeldoutEvaluationV3ObservationRunner:
    """P8 runner that reuses V3 executor receipts and checkpoint transitions."""

    def __init__(
        self,
        *,
        frozen: FrozenPackage7HeldoutInputsV3,
        schedule: Sequence[ScheduledTurnV3],
        checkpoint_path: Path,
        executor_factory: ObservationExecutorFactoryV3,
    ) -> None:
        self.frozen = frozen
        if (
            frozen.protocol_sha256 != EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3
            or evaluation_protocol_sha256_v3(frozen.protocol) != frozen.protocol_sha256
            or frozen.repeat_decision.protocol_sha256 != frozen.protocol_sha256
            or frozen.repeat_decision.repeat_rule_sha256
            != frozen.protocol.repeat_rule_sha256
            or frozen.repeat_decision.selected_repeats not in (2, 3)
        ):
            raise FrozenPackage7DriftError("package7_frozen_input_drift")
        self.schedule = tuple(schedule)
        self.run_id = _schedule_run_id(self.schedule)
        validate_heldout_schedule_v3(frozen, self.schedule, run_id=self.run_id)
        self.schedule_sha256 = pilot_schedule_sha256_v3(self.schedule)
        self.checkpoint_path = checkpoint_path
        self.cases = dict(frozen.heldout_cases)
        self.execution_case_sha256_by_id = {
            case_id: execution_case_sha256_v3(case)
            for case_id, case in self.cases.items()
        }
        self.execution_case_set_sha256 = execution_case_set_sha256_v3(
            self.schedule,
            self.cases,
        )
        self.limits = EvaluationResourceLimitsV3.from_budget(
            frozen.protocol.experiment.budget
        )
        self.executor_factory = executor_factory

    async def run(self) -> EvaluationRunResultV3:
        with CheckpointWriterV3(
            self.checkpoint_path,
            run_id=self.run_id,
            protocol_sha256=self.frozen.protocol_sha256,
            execution_case_set_sha256=self.execution_case_set_sha256,
            schedule_sha256=self.schedule_sha256,
            schedule=self.schedule,
        ) as checkpoint:
            _load_terminal_receipts(
                checkpoint.state,
                schedule=self.schedule,
                schedule_sha256=self.schedule_sha256,
                cases=self.cases,
                execution_case_sha256_by_id=self.execution_case_sha256_by_id,
                execution_case_set_sha256=self.execution_case_set_sha256,
                limits=self.limits,
            )
            turns_by_id = {turn.turn_id: turn for turn in self.schedule}
            orphan_turn_ids = checkpoint.state.orphan_started_turn_ids
            for orphan_turn_id in orphan_turn_ids:
                orphan = turns_by_id[orphan_turn_id]
                ambiguity = AmbiguousObservationReceiptV3(
                    run_id=self.run_id,
                    protocol_sha256=self.frozen.protocol_sha256,
                    execution_case_sha256=self.execution_case_sha256_by_id[
                        orphan.identity.case_id
                    ],
                    execution_case_set_sha256=self.execution_case_set_sha256,
                    schedule_sha256=self.schedule_sha256,
                    schedule_index=orphan.schedule_index,
                    canonical_turn_id=orphan.turn_id,
                    observation_id=orphan.observation_id,
                )
                checkpoint.append_ambiguous(
                    orphan,
                    {"ambiguity": ambiguity.model_dump(mode="json")},
                )

            if orphan_turn_ids:
                return _run_result(
                    checkpoint.state,
                    schedule=self.schedule,
                    run_id=self.run_id,
                    protocol_sha256=self.frozen.protocol_sha256,
                    execution_case_set_sha256=self.execution_case_set_sha256,
                    schedule_sha256=self.schedule_sha256,
                    cases=self.cases,
                    execution_case_sha256_by_id=self.execution_case_sha256_by_id,
                    limits=self.limits,
                )

            terminal_ids = (
                set(checkpoint.state.completed_turn_ids)
                | set(checkpoint.state.failed_turn_ids)
                | set(checkpoint.state.ambiguous_turn_ids)
            )
            for turn in self.schedule:
                if turn.turn_id in terminal_ids:
                    continue
                checkpoint.append_started(turn)
                receipt = await self._execute_observation(turn)
                payload = {"receipt": receipt.model_dump(mode="json")}
                if receipt.terminal_status is ObservationTerminalStatusV3.COMPLETED:
                    checkpoint.append_completed(turn, payload)
                else:
                    checkpoint.append_failed(turn, payload)
                terminal_ids.add(turn.turn_id)

            return _run_result(
                checkpoint.state,
                schedule=self.schedule,
                run_id=self.run_id,
                protocol_sha256=self.frozen.protocol_sha256,
                execution_case_set_sha256=self.execution_case_set_sha256,
                schedule_sha256=self.schedule_sha256,
                cases=self.cases,
                execution_case_sha256_by_id=self.execution_case_sha256_by_id,
                limits=self.limits,
            )

    def seal_orphan_started_observations(self) -> EvaluationRunResultV3:
        """Append only ambiguity receipts for orphaned starts; never dispatch work."""

        with CheckpointWriterV3(
            self.checkpoint_path,
            run_id=self.run_id,
            protocol_sha256=self.frozen.protocol_sha256,
            execution_case_set_sha256=self.execution_case_set_sha256,
            schedule_sha256=self.schedule_sha256,
            schedule=self.schedule,
        ) as checkpoint:
            _load_terminal_receipts(
                checkpoint.state,
                schedule=self.schedule,
                schedule_sha256=self.schedule_sha256,
                cases=self.cases,
                execution_case_sha256_by_id=self.execution_case_sha256_by_id,
                execution_case_set_sha256=self.execution_case_set_sha256,
                limits=self.limits,
            )
            turns_by_id = {turn.turn_id: turn for turn in self.schedule}
            for orphan_turn_id in checkpoint.state.orphan_started_turn_ids:
                orphan = turns_by_id[orphan_turn_id]
                ambiguity = AmbiguousObservationReceiptV3(
                    run_id=self.run_id,
                    protocol_sha256=self.frozen.protocol_sha256,
                    execution_case_sha256=self.execution_case_sha256_by_id[
                        orphan.identity.case_id
                    ],
                    execution_case_set_sha256=self.execution_case_set_sha256,
                    schedule_sha256=self.schedule_sha256,
                    schedule_index=orphan.schedule_index,
                    canonical_turn_id=orphan.turn_id,
                    observation_id=orphan.observation_id,
                )
                checkpoint.append_ambiguous(
                    orphan,
                    {"ambiguity": ambiguity.model_dump(mode="json")},
                )
            return _run_result(
                checkpoint.state,
                schedule=self.schedule,
                run_id=self.run_id,
                protocol_sha256=self.frozen.protocol_sha256,
                execution_case_set_sha256=self.execution_case_set_sha256,
                schedule_sha256=self.schedule_sha256,
                cases=self.cases,
                execution_case_sha256_by_id=self.execution_case_sha256_by_id,
                limits=self.limits,
            )

    async def _execute_observation(
        self,
        turn: ScheduledTurnV3,
    ) -> ObservationRunReceiptV3:
        case = self.cases[turn.identity.case_id]
        context = _execution_context(
            turn,
            execution_case_sha256=self.execution_case_sha256_by_id[case.case_id],
            execution_case_set_sha256=self.execution_case_set_sha256,
            schedule_sha256=self.schedule_sha256,
            limits=self.limits,
        )
        results: list[UserTurnExecutionResultV3] = []
        reset_completed = False
        reset_receipt: InitialStateResetReceiptV3 | None = None
        safe_error_code: str | None = None
        try:
            executor = self.executor_factory(context=context, case=case)
            async with asyncio.timeout(self.limits.turn_deadline_seconds):
                candidate_reset = await executor.reset_initial_state(
                    context=context,
                    case=case,
                )
                if not isinstance(candidate_reset, InitialStateResetReceiptV3):
                    raise ObservationExecutionFailureV3(
                        "initial_state_reset_unacknowledged"
                    )
                expected_reset = initial_state_reset_receipt_v3(context, case)
                if candidate_reset != expected_reset:
                    raise ObservationExecutionFailureV3(
                        "initial_state_reset_binding_invalid"
                    )
                reset_receipt = candidate_reset
                reset_completed = True
            for user_turn in case.user_turns:
                async with asyncio.timeout(self.limits.turn_deadline_seconds):
                    request = _turn_request(context, case, user_turn)
                    result = await executor.execute_turn(request)
                    if not isinstance(result, UserTurnExecutionResultV3):
                        raise ObservationExecutionFailureV3(
                            "executor_receipt_type_invalid"
                        )
                    _validate_turn_result(result, request)
                    candidate_results = (*results, result)
                    _validate_observation_results(
                        candidate_results,
                        context=context,
                        case=case,
                    )
                    results.append(result)
                    _enforce_resource_limits(candidate_results, self.limits)
        except ObservationExecutionFailureV3 as exc:
            safe_error_code = exc.safe_error_code
            try:
                _merge_partial_results(
                    results,
                    exc.partial_results,
                    context=context,
                    case=case,
                )
                _enforce_resource_limits(results, self.limits)
            except ObservationExecutionFailureV3 as partial_error:
                safe_error_code = partial_error.safe_error_code
            except ObservationResourceLimitErrorV3 as partial_error:
                safe_error_code = partial_error.code
        except ObservationResourceLimitErrorV3 as exc:
            safe_error_code = exc.code
        except TimeoutError:
            safe_error_code = "turn_deadline_exceeded"
        except Exception:
            safe_error_code = "observation_executor_failed"

        terminal_status = (
            ObservationTerminalStatusV3.COMPLETED
            if safe_error_code is None
            else ObservationTerminalStatusV3.FAILED
        )
        if terminal_status is ObservationTerminalStatusV3.COMPLETED:
            expected_source_ids = tuple(item.source_turn_id for item in case.user_turns)
            actual_source_ids = tuple(
                item.attribution.source_turn_id for item in results
            )
            if actual_source_ids != expected_source_ids:
                terminal_status = ObservationTerminalStatusV3.FAILED
                safe_error_code = "executor_turn_sequence_invalid"
        return _build_receipt(
            turn,
            context=context,
            terminal_status=terminal_status,
            safe_error_code=safe_error_code,
            initial_state_reset=reset_completed,
            initial_state_reset_receipt=reset_receipt,
            results=tuple(results),
        )


async def run_heldout_benchmark_v3(
    *,
    frozen: FrozenPackage7HeldoutInputsV3,
    schedule: Sequence[ScheduledTurnV3],
    checkpoint_path: Path,
    executor_factory: ObservationExecutorFactoryV3,
) -> EvaluationRunResultV3:
    """Run or resume the P8 held-out schedule through V3 durable contracts."""

    return await HeldoutEvaluationV3ObservationRunner(
        frozen=frozen,
        schedule=schedule,
        checkpoint_path=checkpoint_path,
        executor_factory=executor_factory,
    ).run()


def write_or_validate_execution_plan_v3(
    path: Path,
    plan: HeldoutExecutionPlanV3,
) -> None:
    """Create the P8 plan once or fail if an existing manifest differs."""

    if path.exists():
        current = _load_model(path, HeldoutExecutionPlanV3, "execution_plan")
        validate_heldout_execution_plan_v3(current, plan)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(canonical_json_bytes(plan))
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise FrozenPackage7DriftError("package8_execution_plan_race") from None


def _load_model[T: BaseModel](path: Path, model: type[T], label: str) -> T:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise FrozenPackage7DriftError(f"package7_{label}_invalid") from exc


def _validated_repeat_count_v3(decision: RepeatDecisionV3) -> Literal[2, 3]:
    selected_repeats = decision.selected_repeats
    if selected_repeats not in (2, 3):
        raise FrozenPackage7DriftError("package7_repeat_decision_drift")
    return selected_repeats


def _validate_live_package7_binding(
    project_root: Path,
    protocol: EvaluationProtocolV3,
) -> None:
    """Use P7's own local loader to detect frozen-source or asset drift."""

    from app.evaluation import v3_cli

    arguments = v3_cli._parser().parse_args(
        ("validate", "--project-root", str(project_root))
    )
    current = v3_cli._load_inputs(arguments).protocol
    if current != protocol:
        raise FrozenPackage7DriftError("package7_live_protocol_binding_drift")


def _schedule_run_id(schedule: Sequence[ScheduledTurnV3]) -> str:
    run_ids = {turn.identity.run_id for turn in schedule}
    if len(run_ids) != 1:
        raise FrozenPackage7DriftError("heldout_schedule_run_id_drift")
    return next(iter(run_ids))


def _heldout_case_order(loaded: LoadedEvaluationGoldV3) -> tuple[str, ...]:
    return tuple(
        entry.conversation_id
        for entry in loaded.split.entries
        if entry.split is EvaluationSplitV3.HELD_OUT
    )


__all__ = [
    "EXPECTED_PACKAGE7_PROTOCOL_SHA256_V3",
    "FrozenPackage7DriftError",
    "FrozenPackage7HeldoutInputsV3",
    "HeldoutEvaluationV3ObservationRunner",
    "HeldoutExecutionPlanV3",
    "PACKAGE8_HELDOUT_EXTENSION_ID_V3",
    "PACKAGE8_HELDOUT_SCHEDULE_ID_V3",
    "Package8BenchmarkError",
    "build_heldout_cases_v3",
    "build_heldout_execution_plan_v3",
    "build_heldout_schedule_v3",
    "load_frozen_package7_heldout_inputs_v3",
    "package8_additive_source_sha256_v3",
    "package8_heldout_descriptor_sha256_v3",
    "run_heldout_benchmark_v3",
    "validate_heldout_execution_plan_v3",
    "validate_heldout_schedule_v3",
    "write_or_validate_execution_plan_v3",
]
