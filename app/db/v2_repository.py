"""Owner-scoped durable repository for v2 conversations and turns."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.contracts import AuthorizationContext, TaskStatus
from app.db.base import utc_now
from app.models.v2 import V2Conversation, V2StepResult, V2Turn
from app.v2.authorization import (
    ResourceBinding,
    ResourceNotFoundError,
    authorize_resource_access,
    bind_request_authorization,
)
from app.v2.contracts import (
    CLIENT_TURN_ID_PATTERN,
    IDENTIFIER_PATTERN,
    MAX_DRAFT_REPAIRS,
    MAX_KNOWLEDGE_RETRIEVALS,
    ConversationMode,
    DialogueOutcome,
    SafeExecutionError,
    TurnStatus,
)


class TurnPayloadConflictError(ValueError):
    """A client retry key was reused with a different request payload."""

    code = "turn_payload_conflict"


class StepResultConflictError(ValueError):
    """An operation key was reused for a different durable step result."""

    code = "step_result_conflict"


class TurnStateConflictError(ValueError):
    """A terminal turn was replayed with different terminal state."""

    code = "turn_state_conflict"


class TurnLeaseConflictError(TurnStateConflictError):
    """A guarded write no longer owns the required turn state or lease."""

    code = "turn_lease_conflict"


class TurnLeaseExpiredError(TurnLeaseConflictError):
    """The required turn lease is missing or expired by the database clock."""


class TurnRuntimeConflictError(TurnStateConflictError):
    """Runtime pins, counters or lease do not match the durable turn."""

    code = "turn_runtime_conflict"


class V2Repository:
    """Commit-scoped persistence used before and after provider dispatch.

    Every mutating method commits before returning. This makes the ordering
    boundary explicit: callers can record a turn, dispatch provider work, then
    persist terminal state without relying on a longer-lived outer transaction.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_conversation(
        self,
        authorization: AuthorizationContext,
        *,
        conversation_id: str,
        mode: ConversationMode,
        title: str | None = None,
    ) -> V2Conversation:
        resource_authorization = bind_request_authorization(authorization, mode)
        binding = resource_authorization.binding
        conversation = V2Conversation(
            id=conversation_id,
            tenant_id=binding.tenant_id,
            principal_id=binding.principal_id,
            mode=binding.mode.value,
            store_id=binding.store_id,
            title=title,
        )
        self._session.add(conversation)
        self._session.commit()
        return conversation

    def get_conversation(
        self,
        authorization: AuthorizationContext,
        conversation_id: str,
    ) -> V2Conversation:
        conversation = self._session.scalar(
            select(V2Conversation).where(
                V2Conversation.id == conversation_id,
                V2Conversation.deleted_at.is_(None),
            )
        )
        if conversation is None:
            raise ResourceNotFoundError
        self._authorize(authorization, conversation)
        return conversation

    def list_conversations(
        self,
        authorization: AuthorizationContext,
        *,
        mode: ConversationMode,
        limit: int = 100,
    ) -> Sequence[V2Conversation]:
        if limit < 1 or limit > 100:
            raise ValueError("conversation list limit must be between 1 and 100")
        binding = bind_request_authorization(authorization, mode).binding
        return tuple(
            self._session.scalars(
                select(V2Conversation)
                .where(
                    V2Conversation.tenant_id == binding.tenant_id,
                    V2Conversation.principal_id == binding.principal_id,
                    V2Conversation.mode == binding.mode.value,
                    V2Conversation.store_id == binding.store_id,
                    V2Conversation.deleted_at.is_(None),
                )
                .order_by(V2Conversation.updated_at.desc(), V2Conversation.id.desc())
                .limit(limit)
            )
        )

    def record_turn(
        self,
        authorization: AuthorizationContext,
        *,
        conversation_id: str,
        turn_id: str,
        client_turn_id: str,
        payload: Mapping[str, Any],
        corpus_version_id: str | None = None,
    ) -> V2Turn:
        conversation = self.get_conversation(authorization, conversation_id)
        payload_copy = dict(payload)
        payload_hash = canonical_payload_hash(payload_copy)
        existing = self._turn_for_retry(conversation_id, client_turn_id)
        if existing is not None:
            try:
                replay = _match_turn_replay(existing, payload_hash)
            except TurnPayloadConflictError:
                self._session.rollback()
                raise
            self._session.commit()
            return replay

        turn = V2Turn(
            id=turn_id,
            conversation_id=conversation.id,
            tenant_id=conversation.tenant_id,
            principal_id=conversation.principal_id,
            mode=conversation.mode,
            store_id=conversation.store_id,
            client_turn_id=client_turn_id,
            request_payload_hash=payload_hash,
            request_payload=payload_copy,
            execution_state=TurnStatus.PENDING.value,
            corpus_version_id=corpus_version_id,
        )
        self._session.add(turn)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self._turn_for_retry(conversation_id, client_turn_id)
            if existing is None:
                raise
            try:
                replay = _match_turn_replay(existing, payload_hash)
            except TurnPayloadConflictError:
                self._session.rollback()
                raise
            self._session.commit()
            return replay
        return turn

    def admit_turn(
        self,
        authorization: AuthorizationContext,
        *,
        conversation_id: str,
        client_turn_id: str,
        payload: Mapping[str, Any],
        corpus_version_id: str | None = None,
    ) -> V2Turn:
        """Record the one canonical durable row for a public client turn."""

        return self.record_turn(
            authorization,
            conversation_id=conversation_id,
            turn_id=canonical_turn_id(conversation_id, client_turn_id),
            client_turn_id=client_turn_id,
            payload=payload,
            corpus_version_id=corpus_version_id,
        )

    def get_turn(
        self,
        authorization: AuthorizationContext,
        turn_id: str,
    ) -> V2Turn:
        turn = self._session.scalar(
            select(V2Turn)
            .join(V2Conversation, V2Conversation.id == V2Turn.conversation_id)
            .where(V2Turn.id == turn_id, V2Conversation.deleted_at.is_(None))
        )
        if turn is None:
            raise ResourceNotFoundError
        self._authorize(authorization, turn)
        return turn

    def claim_turn(
        self,
        authorization: AuthorizationContext,
        turn_id: str,
        *,
        lease_owner: str,
        lease_expires_at: datetime | None = None,
        lease_duration: timedelta | None = None,
    ) -> V2Turn:
        if (lease_expires_at is None) == (lease_duration is None):
            raise ValueError("provide exactly one lease expiry or duration")
        if lease_expires_at is not None and (
            lease_expires_at.tzinfo is None or lease_expires_at.utcoffset() is None
        ):
            raise ValueError("lease expiry must be timezone-aware")
        if lease_duration is not None and lease_duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
        turn = self._locked_turn(authorization, turn_id)
        now = self._database_now()
        resolved_expiry = (
            now + lease_duration if lease_duration is not None else lease_expires_at
        )
        assert resolved_expiry is not None
        if resolved_expiry <= now:
            self._session.rollback()
            raise ValueError("lease expiry must be in the future")
        if turn.execution_state != TurnStatus.PENDING.value:
            self._session.rollback()
            raise TurnStateConflictError("turn is not claimable")
        turn.execution_state = TurnStatus.RUNNING.value
        turn.lease_owner = lease_owner
        turn.lease_expires_at = resolved_expiry
        turn.started_at = turn.started_at or now
        turn.completed_at = None
        turn.updated_at = now
        self._session.commit()
        return turn

    def bind_turn_runtime(
        self,
        authorization: AuthorizationContext,
        turn_id: str,
        *,
        data_versions: Mapping[str, str],
    ) -> V2Turn:
        """Bind server-owned snapshot versions once, before claiming a turn."""

        versions = _validated_runtime_versions(data_versions)
        turn = self._locked_turn(authorization, turn_id)
        try:
            if turn.execution_state != TurnStatus.PENDING.value:
                raise TurnRuntimeConflictError("only pending turns may bind runtime")
            if turn.corpus_version_id != versions["corpus_version_id"]:
                raise TurnRuntimeConflictError("runtime corpus pin does not match")
            if turn.runtime_metadata:
                metadata = _validated_runtime_metadata(turn.runtime_metadata)
                if metadata["data_versions"] != versions:
                    raise TurnRuntimeConflictError("turn runtime is already bound")
            else:
                turn.runtime_metadata = {
                    "schema_version": 1,
                    "data_versions": versions,
                    "knowledge_retrievals": 0,
                    "draft_repairs": 0,
                }
                turn.updated_at = utc_now()
            self._session.commit()
            return turn
        except Exception:
            self._session.rollback()
            raise

    def checkpoint_turn_runtime(
        self,
        authorization: AuthorizationContext,
        turn_id: str,
        *,
        lease_owner: str,
        knowledge_retrievals: int | None = None,
        draft_repairs: int | None = None,
    ) -> V2Turn:
        """Commit monotonic attempted-work counters before provider dispatch."""

        for value, limit in (
            (knowledge_retrievals, MAX_KNOWLEDGE_RETRIEVALS),
            (draft_repairs, MAX_DRAFT_REPAIRS),
        ):
            if value is not None and (
                type(value) is not int or not 0 <= value <= limit
            ):
                raise ValueError("runtime counter is outside its strict integer limit")
        turn = self._locked_turn(authorization, turn_id)
        try:
            expiry = turn.lease_expires_at
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            now = self._database_now()
            if (
                turn.execution_state != TurnStatus.RUNNING.value
                or turn.lease_owner != lease_owner
                or expiry is None
                or expiry <= now
            ):
                raise TurnRuntimeConflictError("turn runtime lease is not current")
            metadata = _validated_runtime_metadata(turn.runtime_metadata)
            for field, value in (
                ("knowledge_retrievals", knowledge_retrievals),
                ("draft_repairs", draft_repairs),
            ):
                if value is None:
                    continue
                if value < metadata[field]:
                    raise TurnRuntimeConflictError("runtime counters cannot decrease")
                metadata[field] = value
            turn.runtime_metadata = metadata
            turn.updated_at = now
            self._session.commit()
            return turn
        except Exception:
            self._session.rollback()
            raise

    def assert_turn_claim(
        self,
        authorization: AuthorizationContext,
        turn_id: str,
        *,
        lease_owner: str,
    ) -> None:
        """Check an active worker's lease against the DB clock under the row lock."""

        turn = self._locked_turn(authorization, turn_id)
        try:
            self._validate_turn_write_fence(turn, lease_owner=lease_owner)
        finally:
            self._session.rollback()

    def turn_claim_is_current(
        self,
        authorization: AuthorizationContext,
        turn_id: str,
        *,
        lease_owner: str,
    ) -> bool:
        """Return whether a worker still owns a live claim by the database clock."""

        turn = self._locked_turn(authorization, turn_id)
        try:
            self._validate_turn_write_fence(turn, lease_owner=lease_owner)
        except TurnLeaseConflictError:
            return False
        finally:
            self._session.rollback()
        return True

    def persist_step_result(
        self,
        authorization: AuthorizationContext,
        *,
        turn_id: str,
        step_result_id: str,
        operation_key: str,
        status: TaskStatus,
        result: Mapping[str, Any],
        plan_revision: int = 0,
        data_version: str | None = None,
        lease_owner: str | None = None,
    ) -> V2StepResult:
        if status not in {
            TaskStatus.SUCCESS,
            TaskStatus.PARTIAL_SUCCESS,
            TaskStatus.FAILED,
        }:
            raise ValueError("only terminal step results are durable")
        turn = self._locked_turn(authorization, turn_id)
        result_copy = dict(result)
        existing = self._session.scalar(
            select(V2StepResult).where(
                V2StepResult.turn_id == turn_id,
                V2StepResult.operation_key == operation_key,
            )
        )
        if existing is not None:
            try:
                replay = _match_step_replay(
                    existing,
                    status=status,
                    result=result_copy,
                    plan_revision=plan_revision,
                    data_version=data_version,
                )
            except StepResultConflictError:
                self._session.rollback()
                raise
            self._session.commit()
            return replay

        self._validate_turn_write_fence(turn, lease_owner=lease_owner)
        step_result = V2StepResult(
            id=step_result_id,
            turn_id=turn.id,
            conversation_id=turn.conversation_id,
            operation_key=operation_key,
            plan_revision=plan_revision,
            status=status.value,
            result=result_copy,
            data_version=data_version,
            completed_at=utc_now(),
        )
        self._session.add(step_result)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self._session.scalar(
                select(V2StepResult).where(
                    V2StepResult.turn_id == turn_id,
                    V2StepResult.operation_key == operation_key,
                )
            )
            if existing is None:
                raise
            try:
                replay = _match_step_replay(
                    existing,
                    status=status,
                    result=result_copy,
                    plan_revision=plan_revision,
                    data_version=data_version,
                )
            except StepResultConflictError:
                self._session.rollback()
                raise
            self._session.commit()
            return replay
        return step_result

    def get_step_result(
        self,
        authorization: AuthorizationContext,
        *,
        turn_id: str,
        operation_key: str,
    ) -> V2StepResult | None:
        self.get_turn(authorization, turn_id)
        return self._session.scalar(
            select(V2StepResult).where(
                V2StepResult.turn_id == turn_id,
                V2StepResult.operation_key == operation_key,
            )
        )

    def list_step_results(
        self,
        authorization: AuthorizationContext,
        turn_id: str,
    ) -> Sequence[V2StepResult]:
        self.get_turn(authorization, turn_id)
        return tuple(
            self._session.scalars(
                select(V2StepResult)
                .where(V2StepResult.turn_id == turn_id)
                .order_by(V2StepResult.created_at, V2StepResult.id)
            )
        )

    def complete_turn(
        self,
        authorization: AuthorizationContext,
        turn_id: str,
        *,
        status: TurnStatus,
        dialogue_outcome: DialogueOutcome | None = None,
        result: Mapping[str, Any] | None = None,
        safe_error: SafeExecutionError | None = None,
        lease_owner: str | None = None,
        expected_status: TurnStatus | None = None,
        require_expired_lease: bool = False,
    ) -> V2Turn:
        if lease_owner is not None and require_expired_lease:
            raise ValueError(
                "an active-worker lease cannot be combined with expired recovery"
            )
        terminal = {
            TurnStatus.COMPLETED,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
        }
        if status not in terminal:
            raise ValueError("terminal persistence requires a terminal turn status")
        if status is TurnStatus.COMPLETED:
            if dialogue_outcome is None or result is None or safe_error is not None:
                raise ValueError("completed turns require an outcome and result")
        elif dialogue_outcome is not None or result is not None:
            raise ValueError("only completed turns may persist a dialogue result")
        if status in {TurnStatus.FAILED, TurnStatus.INTERRUPTED}:
            if safe_error is None:
                raise ValueError("failed and interrupted turns require a safe error")
        elif safe_error is not None:
            raise ValueError(
                "safe errors are only valid for failed or interrupted turns"
            )

        turn = self._locked_turn(authorization, turn_id)
        result_copy = dict(result) if result is not None else None
        error_copy = (
            safe_error.model_dump(mode="json") if safe_error is not None else None
        )
        if turn.execution_state in {item.value for item in terminal}:
            if (
                turn.execution_state == status.value
                and turn.dialogue_outcome
                == (dialogue_outcome.value if dialogue_outcome is not None else None)
                and turn.result == result_copy
                and turn.safe_error == error_copy
            ):
                self._session.commit()
                return turn
            self._session.rollback()
            raise TurnStateConflictError("turn already has different terminal state")

        self._validate_turn_write_fence(
            turn,
            lease_owner=lease_owner,
            expected_status=expected_status,
            require_expired_lease=require_expired_lease,
        )
        now = utc_now()
        turn.execution_state = status.value
        turn.dialogue_outcome = (
            dialogue_outcome.value if dialogue_outcome is not None else None
        )
        turn.result = result_copy
        turn.safe_error = error_copy
        turn.completed_at = now
        turn.updated_at = now
        turn.lease_owner = None
        turn.lease_expires_at = None
        self._session.commit()
        return turn

    def cancel_turn(
        self,
        authorization: AuthorizationContext,
        turn_id: str,
    ) -> V2Turn:
        """Cancel pending/running work under its row lock and replay the winner."""

        terminal = {
            TurnStatus.COMPLETED.value,
            TurnStatus.FAILED.value,
            TurnStatus.CANCELLED.value,
            TurnStatus.INTERRUPTED.value,
        }
        turn = self._locked_turn(authorization, turn_id)
        if turn.execution_state in terminal:
            self._session.commit()
            return turn
        if turn.execution_state not in {
            TurnStatus.PENDING.value,
            TurnStatus.RUNNING.value,
        }:
            self._session.rollback()
            raise TurnStateConflictError("turn has an invalid cancellable state")

        now = self._database_now()
        turn.execution_state = TurnStatus.CANCELLED.value
        turn.dialogue_outcome = None
        turn.result = None
        turn.safe_error = None
        turn.completed_at = now
        turn.updated_at = now
        turn.lease_owner = None
        turn.lease_expires_at = None
        self._session.commit()
        return turn

    def _turn_for_retry(
        self,
        conversation_id: str,
        client_turn_id: str,
    ) -> V2Turn | None:
        return self._session.scalar(
            select(V2Turn).where(
                V2Turn.conversation_id == conversation_id,
                V2Turn.client_turn_id == client_turn_id,
            )
        )

    def _locked_turn(
        self,
        authorization: AuthorizationContext,
        turn_id: str,
    ) -> V2Turn:
        turn = self._session.scalar(
            select(V2Turn)
            .join(V2Conversation, V2Conversation.id == V2Turn.conversation_id)
            .where(V2Turn.id == turn_id, V2Conversation.deleted_at.is_(None))
            .with_for_update(of=V2Turn)
            .execution_options(populate_existing=True)
        )
        if turn is None:
            self._session.rollback()
            raise ResourceNotFoundError
        try:
            self._authorize(authorization, turn)
        except Exception:
            self._session.rollback()
            raise
        return turn

    def _validate_turn_write_fence(
        self,
        turn: V2Turn,
        *,
        lease_owner: str | None = None,
        expected_status: TurnStatus | None = None,
        require_expired_lease: bool = False,
    ) -> None:
        """Validate an optional state/lease premise while holding the turn lock."""

        expiry = turn.lease_expires_at
        if expiry is not None and (expiry.tzinfo is None or expiry.utcoffset() is None):
            expiry = expiry.replace(tzinfo=UTC)
        database_now = (
            self._database_now()
            if lease_owner is not None or require_expired_lease
            else None
        )
        conflict: str | None = None
        lease_expired = False
        if (
            expected_status is not None
            and turn.execution_state != expected_status.value
        ):
            conflict = "turn no longer has the expected state"
        elif lease_owner is not None:
            if (
                turn.execution_state != TurnStatus.RUNNING.value
                or turn.lease_owner != lease_owner
            ):
                conflict = "turn lease is not current for this worker"
            elif expiry is None or database_now is None or expiry <= database_now:
                conflict = "turn lease is not current for this worker: expired"
                lease_expired = True
        elif require_expired_lease and (
            turn.execution_state != TurnStatus.RUNNING.value
            or database_now is None
            or (expiry is not None and expiry > database_now)
        ):
            conflict = "turn does not have an expired or missing running lease"
        if conflict is not None:
            self._session.rollback()
            error_type = (
                TurnLeaseExpiredError if lease_expired else TurnLeaseConflictError
            )
            raise error_type(conflict)

    def _database_now(self) -> datetime:
        value = self._session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):
            self._session.rollback()
            raise TurnLeaseConflictError("database clock is unavailable")
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value

    @staticmethod
    def _authorize(
        authorization: AuthorizationContext,
        resource: V2Conversation | V2Turn,
    ) -> None:
        authorize_resource_access(
            authorization,
            ResourceBinding(
                tenant_id=resource.tenant_id,
                principal_id=resource.principal_id,
                mode=ConversationMode(resource.mode),
                store_id=resource.store_id,
            ),
        )


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    """Hash a canonical JSON payload for deterministic retry comparison."""

    canonical = json.dumps(
        dict(payload),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def canonical_turn_id(conversation_id: str, client_turn_id: str) -> str:
    """Derive the stable server-owned turn identity used by retries and budgets."""

    if re.fullmatch(IDENTIFIER_PATTERN, conversation_id) is None:
        raise ValueError("conversation_id must be a stable identifier")
    if re.fullmatch(CLIENT_TURN_ID_PATTERN, client_turn_id) is None:
        raise ValueError("client_turn_id must be a valid retry identifier")
    canonical = json.dumps(
        [conversation_id, client_turn_id],
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"turn_{hashlib.sha256(canonical).hexdigest()[:48]}"


def _validated_runtime_versions(data_versions: Mapping[str, str]) -> dict[str, str]:
    if set(data_versions) != {
        "catalog_version_id",
        "corpus_version_id",
        "index_manifest_id",
    }:
        raise ValueError("runtime binding requires exactly three snapshot versions")
    if any(
        not isinstance(value, str) or re.fullmatch(IDENTIFIER_PATTERN, value) is None
        for value in data_versions.values()
    ):
        raise ValueError("runtime snapshot versions must be stable identifiers")
    return dict(data_versions)


def _validated_runtime_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    if (
        set(value)
        != {"schema_version", "data_versions", "knowledge_retrievals", "draft_repairs"}
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise TurnRuntimeConflictError("turn runtime metadata is not bound or valid")
    versions = value["data_versions"]
    if not isinstance(versions, Mapping):
        raise TurnRuntimeConflictError("turn runtime versions are invalid")
    try:
        versions = _validated_runtime_versions(versions)
    except ValueError as exc:
        raise TurnRuntimeConflictError("turn runtime versions are invalid") from exc
    for field, limit in (
        ("knowledge_retrievals", MAX_KNOWLEDGE_RETRIEVALS),
        ("draft_repairs", MAX_DRAFT_REPAIRS),
    ):
        if type(value[field]) is not int or not 0 <= value[field] <= limit:
            raise TurnRuntimeConflictError("turn runtime counters are invalid")
    return {**value, "data_versions": versions}


def _match_turn_replay(turn: V2Turn, payload_hash: str) -> V2Turn:
    if turn.request_payload_hash != payload_hash:
        raise TurnPayloadConflictError(
            "client_turn_id was already used with a different payload"
        )
    return turn


def _match_step_replay(
    step: V2StepResult,
    *,
    status: TaskStatus,
    result: Mapping[str, Any],
    plan_revision: int,
    data_version: str | None,
) -> V2StepResult:
    if (
        step.status != _enum_value(status)
        or step.result != dict(result)
        or step.plan_revision != plan_revision
        or step.data_version != data_version
    ):
        raise StepResultConflictError(
            "operation_key was already used with a different step result"
        )
    return step


def _enum_value(value: Enum) -> str:
    return str(value.value)
