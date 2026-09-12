"""Owner-scoped durable v2 conversation history and memory service."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Annotated, Callable, overload

from pydantic import (
    AwareDatetime,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.contracts import AuthorizationContext, TaskStatus
from app.db.base import utc_now
from app.models.v2 import (
    V2Conversation,
    V2Preference,
    V2Proposal,
    V2StepResult,
    V2Turn,
)
from app.v2.authorization import (
    ResourceBinding,
    ResourceNotFoundError,
    authorize_resource_access,
    bind_request_authorization,
)
from app.v2.contracts import (
    ACTION_PATTERN,
    CLIENT_TURN_ID_PATTERN,
    IDENTIFIER_PATTERN,
    MAX_MESSAGE_LENGTH,
    ActionCard,
    ConversationMode,
    ConversationSummary,
    DialogueOutcome,
    PreferenceDeleteRequest,
    PreferenceKind,
    PreferencePutRequest,
    PreferenceRecord,
    PreferenceValue,
    ProductId,
    SafeExecutionError,
    TurnResult,
    TurnStatus,
    V2Contract,
)
from app.v2.runtime_contracts import ExpertResult

MAX_CONVERSATION_LIST = 100
MAX_HISTORY_TURNS = 100
MAX_CONTEXT_TURNS = 8
MAX_CONTEXT_CONSTRAINTS = 16
MAX_CONTEXT_PRODUCTS = 8
MAX_CONTEXT_PREFERENCES = 4
_PRODUCT_SUBJECT_PATTERN = re.compile(r"^product_(?P<product_id>[1-9][0-9]*)$")

ContextValue = Annotated[
    StrictStr | StrictInt | StrictBool,
    Field(union_mode="left_to_right"),
]
ConstraintParser = Callable[[str], Collection["ContextConstraint"]]


class HistoryDataError(ValueError):
    """A durable row cannot be represented by the strict history contract."""

    code = "history_data_invalid"


class PreferenceSourceError(ValueError):
    """A preference was not sourced from a live completed owner turn."""

    code = "preference_source_invalid"


class ContextConstraint(V2Contract):
    """One validated scalar constraint supplied by the current request."""

    key: str = Field(pattern=ACTION_PATTERN, max_length=80)
    value: ContextValue


class HistoryTurn(V2Contract):
    """Safe public history projection without raw runtime or tool payloads."""

    turn_id: str = Field(pattern=IDENTIFIER_PATTERN)
    client_turn_id: str = Field(pattern=CLIENT_TURN_ID_PATTERN)
    status: TurnStatus
    outcome: DialogueOutcome | None = None
    user_message: str | None = Field(default=None, max_length=MAX_MESSAGE_LENGTH)
    assistant_result: TurnResult | None = None
    error: SafeExecutionError | None = None
    action_cards: tuple[ActionCard, ...] = ()
    created_at: AwareDatetime
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_state(self) -> HistoryTurn:
        terminal = {
            TurnStatus.COMPLETED,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
        }
        if self.status == TurnStatus.COMPLETED:
            if self.assistant_result is None or self.outcome is None:
                raise ValueError("completed history turns require a result")
            if self.error is not None:
                raise ValueError("completed history turns cannot expose an error")
            if self.outcome != self.assistant_result.outcome:
                raise ValueError("history outcome and result outcome must match")
            if self.action_cards != self.assistant_result.action_cards:
                raise ValueError("history action cards and result action cards differ")
        elif self.assistant_result is not None or self.outcome is not None:
            raise ValueError("only completed history turns expose results")

        if self.status in {TurnStatus.FAILED, TurnStatus.INTERRUPTED}:
            if self.error is None:
                raise ValueError("failed history turns require a safe error")
        elif self.error is not None:
            raise ValueError("this history state cannot expose an error")

        if self.status in terminal and self.completed_at is None:
            raise ValueError("terminal history turns require completed_at")
        if self.status not in terminal and self.completed_at is not None:
            raise ValueError("non-terminal history turns cannot have completed_at")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("history completion cannot precede creation")
        return self


class ModelContext(V2Contract):
    """Bounded model input assembled from durable rows and current request data."""

    recent_turns: tuple[HistoryTurn, ...] = Field(
        default=(), max_length=MAX_CONTEXT_TURNS
    )
    active_constraints: tuple[ContextConstraint, ...] = Field(
        default=(), max_length=MAX_CONTEXT_CONSTRAINTS
    )
    referenced_product_ids: tuple[ProductId, ...] = Field(
        default=(), max_length=MAX_CONTEXT_PRODUCTS
    )
    preferences: tuple[PreferenceRecord, ...] = Field(
        default=(), max_length=MAX_CONTEXT_PREFERENCES
    )

    @model_validator(mode="after")
    def validate_unique_context_values(self) -> ModelContext:
        keys = [constraint.key for constraint in self.active_constraints]
        if len(keys) != len(set(keys)):
            raise ValueError("context constraints must have unique keys")
        if len(self.referenced_product_ids) != len(set(self.referenced_product_ids)):
            raise ValueError("context product IDs must be unique")
        preference_ids = [item.preference_id for item in self.preferences]
        if len(preference_ids) != len(set(preference_ids)):
            raise ValueError("context preferences must be unique")
        return self


_PREFERENCE_ADAPTER: TypeAdapter[PreferenceValue] = TypeAdapter(PreferenceValue)


class V2HistoryService:
    """Read/write boundary for durable owner-scoped v2 history and memory."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_conversations(
        self,
        authorization: AuthorizationContext,
        *,
        mode: ConversationMode,
        limit: int = MAX_CONVERSATION_LIST,
    ) -> tuple[ConversationSummary, ...]:
        _validate_limit(limit, MAX_CONVERSATION_LIST, "conversation list limit")
        binding = bind_request_authorization(
            authorization, ConversationMode(mode)
        ).binding
        rows = tuple(
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
        return tuple(_conversation_summary(row) for row in rows)

    def list_turns(
        self,
        authorization: AuthorizationContext,
        conversation_id: str,
        *,
        limit: int = MAX_HISTORY_TURNS,
    ) -> tuple[HistoryTurn, ...]:
        _validate_limit(limit, MAX_HISTORY_TURNS, "history turn limit")
        conversation = self._live_conversation(authorization, conversation_id)
        rows = tuple(
            self._session.scalars(
                select(V2Turn)
                .where(V2Turn.conversation_id == conversation.id)
                .order_by(V2Turn.created_at.desc(), V2Turn.id.desc())
                .limit(limit)
            )
        )
        return tuple(_history_turn(row) for row in reversed(rows))

    def list_preferences(
        self,
        authorization: AuthorizationContext,
        *,
        mode: ConversationMode,
        preference_keys: Collection[PreferenceKind] | None = None,
    ) -> tuple[PreferenceRecord, ...]:
        mode = ConversationMode(mode)
        binding = bind_request_authorization(authorization, mode).binding
        keys = _preference_keys(preference_keys)
        statement = (
            select(V2Preference)
            .join(
                V2Conversation,
                V2Conversation.id == V2Preference.conversation_id,
            )
            .where(
                V2Preference.tenant_id == binding.tenant_id,
                V2Preference.principal_id == binding.principal_id,
                V2Preference.mode == binding.mode.value,
                V2Preference.store_id == binding.store_id,
                V2Conversation.deleted_at.is_(None),
            )
            .order_by(V2Preference.preference_key, V2Preference.id)
        )
        if keys is not None:
            statement = statement.where(V2Preference.preference_key.in_(keys))
        rows = tuple(self._session.scalars(statement))
        return tuple(_preference_record(row) for row in rows)

    def put_preference(
        self,
        authorization: AuthorizationContext,
        request: PreferencePutRequest,
    ) -> PreferenceRecord:
        """Upsert one explicitly requested preference tied to a completed turn."""

        try:
            source_turn = self._live_turn(
                authorization,
                request.source_turn_id,
                for_update=True,
                write=True,
            )
            if source_turn.execution_state != TurnStatus.COMPLETED.value:
                raise PreferenceSourceError("preference source turn must be completed")
            preference = request.preference
            key = preference.kind.value
            payload = preference.model_dump(mode="json")
            row = self._session.scalar(
                select(V2Preference)
                .where(
                    V2Preference.tenant_id == source_turn.tenant_id,
                    V2Preference.principal_id == source_turn.principal_id,
                    V2Preference.mode == source_turn.mode,
                    V2Preference.store_id == source_turn.store_id,
                    V2Preference.preference_key == key,
                )
                .with_for_update()
            )
            now = utc_now()
            if row is None:
                row = V2Preference(
                    id=_preference_id(source_turn, key),
                    tenant_id=source_turn.tenant_id,
                    principal_id=source_turn.principal_id,
                    mode=source_turn.mode,
                    store_id=source_turn.store_id,
                    conversation_id=source_turn.conversation_id,
                    source_turn_id=source_turn.id,
                    preference_key=key,
                    preference_value=payload,
                    created_at=now,
                    updated_at=now,
                )
                self._session.add(row)
            else:
                row.conversation_id = source_turn.conversation_id
                row.source_turn_id = source_turn.id
                row.preference_value = payload
                row.updated_at = now
            try:
                self._session.commit()
            except IntegrityError:
                # A concurrent first write can win the owner/key unique key.
                # Re-read its row and apply this explicit write deterministically.
                self._session.rollback()
                row = self._session.scalar(
                    select(V2Preference)
                    .where(
                        V2Preference.tenant_id == source_turn.tenant_id,
                        V2Preference.principal_id == source_turn.principal_id,
                        V2Preference.mode == source_turn.mode,
                        V2Preference.store_id == source_turn.store_id,
                        V2Preference.preference_key == key,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise
                row.conversation_id = source_turn.conversation_id
                row.source_turn_id = source_turn.id
                row.preference_value = payload
                row.updated_at = utc_now()
                self._session.commit()
            return _preference_record(row)
        except Exception:
            self._session.rollback()
            raise

    def delete_preference(
        self,
        authorization: AuthorizationContext,
        request: PreferenceDeleteRequest,
        *,
        mode: ConversationMode,
    ) -> None:
        """Delete one current owner-scoped preference."""

        try:
            mode = ConversationMode(mode)
            bind_request_authorization(authorization, mode, write=True)
            row = self._session.scalar(
                select(V2Preference).where(V2Preference.id == request.preference_id)
            )
            if row is None:
                raise ResourceNotFoundError
            _authorize_binding(
                authorization,
                _binding_for(row),
                write=True,
            )
            if row.mode != mode.value:
                raise ResourceNotFoundError
            self._live_conversation(
                authorization,
                row.conversation_id,
                for_update=True,
                write=True,
            )
            row = self._session.scalar(
                select(V2Preference)
                .where(V2Preference.id == request.preference_id)
                .with_for_update()
            )
            if row is None:
                raise ResourceNotFoundError
            self._session.delete(row)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def build_model_context(
        self,
        authorization: AuthorizationContext,
        conversation_id: str,
        *,
        current_constraints: Collection[ContextConstraint] = (),
        current_referenced_product_ids: Collection[ProductId] = (),
        relevant_preference_keys: Collection[PreferenceKind] | None = None,
        turn_limit: int = MAX_CONTEXT_TURNS,
        constraint_parser: ConstraintParser | None = None,
    ) -> ModelContext:
        """Build bounded context, overlaying validated current request data last."""

        _validate_limit(turn_limit, MAX_CONTEXT_TURNS, "context turn limit")
        conversation = self._live_conversation(authorization, conversation_id)
        constraints = tuple(
            item
            if isinstance(item, ContextConstraint)
            else ContextConstraint.model_validate(item)
            for item in current_constraints
        )
        _validate_unique_constraint_keys(constraints)
        product_ids = _validated_product_ids(current_referenced_product_ids)
        preferences = self.list_preferences(
            authorization,
            mode=ConversationMode(conversation.mode),
            preference_keys=relevant_preference_keys,
        )
        rows = tuple(
            self._session.scalars(
                select(V2Turn)
                .where(
                    V2Turn.conversation_id == conversation.id,
                    V2Turn.execution_state == TurnStatus.COMPLETED.value,
                )
                .order_by(V2Turn.created_at.desc(), V2Turn.id.desc())
                .limit(turn_limit)
            )
        )
        recent_turns = tuple(_history_turn(row) for row in reversed(rows))
        historical_constraints: tuple[ContextConstraint, ...] = ()
        if constraint_parser is not None:
            for turn in recent_turns:
                if turn.user_message is None:
                    continue
                try:
                    parsed = tuple(constraint_parser(turn.user_message))
                    _validate_unique_constraint_keys(parsed)
                except (TypeError, ValueError, RuntimeError):
                    continue
                historical_constraints = _overlay_constraints(
                    historical_constraints,
                    parsed,
                )
        preference_constraints = tuple(
            ContextConstraint(
                key=record.preference.kind.value,
                value=record.preference.value,
            )
            for record in preferences
        )
        active_constraints = _overlay_constraints(
            preference_constraints,
            historical_constraints,
        )
        active_constraints = _overlay_constraints(
            active_constraints,
            constraints,
        )
        referenced_product_ids = product_ids or self._historical_product_ids(rows)
        return ModelContext(
            recent_turns=recent_turns,
            active_constraints=active_constraints,
            referenced_product_ids=referenced_product_ids,
            preferences=preferences,
        )

    def _historical_product_ids(
        self,
        completed_turns_newest_first: tuple[V2Turn, ...],
    ) -> tuple[ProductId, ...]:
        product_ids: list[ProductId] = []
        seen: set[int] = set()
        for turn in completed_turns_newest_first:
            rows = tuple(
                self._session.scalars(
                    select(V2StepResult)
                    .where(V2StepResult.turn_id == turn.id)
                    .order_by(V2StepResult.id)
                )
            )
            for row in rows:
                if not isinstance(row.result, Mapping):
                    continue
                try:
                    result = ExpertResult.model_validate(row.result)
                except (TypeError, ValueError):
                    continue
                if result.status not in {
                    TaskStatus.SUCCESS,
                    TaskStatus.PARTIAL_SUCCESS,
                }:
                    continue
                for fact in result.evidence.facts:
                    subject_id = fact.subject_id
                    if subject_id is None:
                        continue
                    match = _PRODUCT_SUBJECT_PATTERN.fullmatch(subject_id)
                    if match is None:
                        continue
                    product_id = int(match.group("product_id"))
                    if product_id in seen:
                        continue
                    seen.add(product_id)
                    product_ids.append(product_id)
                    if len(product_ids) == MAX_CONTEXT_PRODUCTS:
                        return tuple(product_ids)
        return tuple(product_ids)

    def delete_conversation(
        self,
        authorization: AuthorizationContext,
        conversation_id: str,
    ) -> ConversationSummary:
        """Atomically expire pending proposals, remove memory, and soft-delete."""

        try:
            conversation = self._live_conversation(
                authorization,
                conversation_id,
                for_update=True,
                write=True,
            )
            proposals = tuple(
                self._session.scalars(
                    select(V2Proposal)
                    .where(
                        V2Proposal.conversation_id == conversation.id,
                        V2Proposal.tenant_id == conversation.tenant_id,
                        V2Proposal.principal_id == conversation.principal_id,
                        V2Proposal.mode == conversation.mode,
                        V2Proposal.store_id == conversation.store_id,
                        V2Proposal.status.in_(("proposed", "confirmed")),
                    )
                    .with_for_update()
                )
            )
            now = utc_now()
            for proposal in proposals:
                proposal.status = "expired"
                proposal.result = {"code": "conversation_deleted"}
                proposal.resolved_at = now
            self._session.execute(
                delete(V2Preference).where(
                    V2Preference.conversation_id == conversation.id,
                    V2Preference.tenant_id == conversation.tenant_id,
                    V2Preference.principal_id == conversation.principal_id,
                    V2Preference.mode == conversation.mode,
                    V2Preference.store_id == conversation.store_id,
                )
            )
            conversation.deleted_at = now
            conversation.updated_at = now
            self._session.commit()
            return _conversation_summary(conversation)
        except Exception:
            self._session.rollback()
            raise

    def _live_conversation(
        self,
        authorization: AuthorizationContext,
        conversation_id: str,
        *,
        for_update: bool = False,
        write: bool = False,
    ) -> V2Conversation:
        statement = select(V2Conversation).where(
            V2Conversation.id == conversation_id,
            V2Conversation.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        conversation = self._session.scalar(statement)
        if conversation is None:
            raise ResourceNotFoundError
        _authorize_binding(authorization, _binding_for(conversation), write=write)
        return conversation

    def _live_turn(
        self,
        authorization: AuthorizationContext,
        turn_id: str,
        *,
        for_update: bool = False,
        write: bool = False,
    ) -> V2Turn:
        statement = (
            select(V2Turn)
            .join(V2Conversation, V2Conversation.id == V2Turn.conversation_id)
            .where(
                V2Turn.id == turn_id,
                V2Conversation.deleted_at.is_(None),
            )
        )
        if for_update:
            statement = statement.with_for_update(of=V2Conversation)
        turn = self._session.scalar(statement)
        if turn is None:
            raise ResourceNotFoundError
        _authorize_binding(authorization, _binding_for(turn), write=write)
        return turn


def _conversation_summary(conversation: V2Conversation) -> ConversationSummary:
    return ConversationSummary(
        conversation_id=conversation.id,
        mode=ConversationMode(conversation.mode),
        store_id=conversation.store_id,
        title=conversation.title,
        created_at=_aware(conversation.created_at),
        updated_at=_aware(conversation.updated_at),
    )


def _history_turn(turn: V2Turn) -> HistoryTurn:
    payload = turn.request_payload
    if not isinstance(payload, Mapping):
        raise HistoryDataError("turn request payload is not an object")
    message = payload.get("message")
    if message is not None and (
        not isinstance(message, str) or len(message) > MAX_MESSAGE_LENGTH
    ):
        raise HistoryDataError("turn message is not valid")
    try:
        status = TurnStatus(turn.execution_state)
    except ValueError as exc:
        raise HistoryDataError("turn execution state is invalid") from exc
    result = _turn_result(turn.result)
    error = _safe_error(turn.safe_error)
    return HistoryTurn(
        turn_id=turn.id,
        client_turn_id=turn.client_turn_id,
        status=status,
        outcome=result.outcome if result is not None else None,
        user_message=message,
        assistant_result=result,
        error=error,
        action_cards=result.action_cards if result is not None else (),
        created_at=_aware(turn.created_at),
        completed_at=_aware(turn.completed_at),
    )


def _turn_result(value: object) -> TurnResult | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise HistoryDataError("turn result is not an object")
    candidate = value.get("turn_result", value)
    if not isinstance(candidate, Mapping):
        raise HistoryDataError("turn result envelope is invalid")
    try:
        return TurnResult.model_validate(candidate)
    except ValidationError as exc:
        raise HistoryDataError("turn result failed public validation") from exc


def _safe_error(value: object) -> SafeExecutionError | None:
    if value is None:
        return None
    try:
        return SafeExecutionError.model_validate(value)
    except ValidationError as exc:
        raise HistoryDataError("turn safe error failed public validation") from exc


def _preference_record(row: V2Preference) -> PreferenceRecord:
    if not isinstance(row.preference_value, Mapping):
        raise HistoryDataError("preference value is not an object")
    try:
        preference = _PREFERENCE_ADAPTER.validate_python(row.preference_value)
    except ValidationError as exc:
        raise HistoryDataError("preference failed public validation") from exc
    if preference.kind.value != row.preference_key:
        raise HistoryDataError("preference key and value kind differ")
    return PreferenceRecord(
        preference_id=row.id,
        source_turn_id=row.source_turn_id,
        preference=preference,
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def _binding_for(resource: V2Conversation | V2Turn | V2Preference) -> ResourceBinding:
    return ResourceBinding(
        tenant_id=resource.tenant_id,
        principal_id=resource.principal_id,
        mode=ConversationMode(resource.mode),
        store_id=resource.store_id,
    )


def _authorize_binding(
    authorization: AuthorizationContext,
    binding: ResourceBinding,
    *,
    write: bool = False,
) -> None:
    authorize_resource_access(authorization, binding, write=write)


def _preference_id(turn: V2Turn, key: str) -> str:
    scope = "\x1f".join(
        (turn.tenant_id, turn.principal_id, turn.mode, turn.store_id, key)
    )
    return f"preference_{hashlib.sha256(scope.encode('utf-8')).hexdigest()[:40]}"


def _preference_keys(
    values: Collection[PreferenceKind] | None,
) -> tuple[str, ...] | None:
    if values is None:
        return None
    normalized = tuple(PreferenceKind(value) for value in values)
    keys = tuple(item.value for item in normalized)
    if len(keys) != len(set(keys)):
        raise ValueError("preference keys must be unique")
    return keys


def _overlay_constraints(
    base: tuple[ContextConstraint, ...],
    current: tuple[ContextConstraint, ...],
) -> tuple[ContextConstraint, ...]:
    _validate_unique_constraint_keys(base)
    _validate_unique_constraint_keys(current)
    merged = list(base)
    positions = {item.key: index for index, item in enumerate(merged)}
    for item in current:
        index = positions.get(item.key)
        if index is None:
            positions[item.key] = len(merged)
            merged.append(item)
        else:
            merged[index] = item
    return tuple(merged)


def _validate_unique_constraint_keys(
    constraints: Collection[ContextConstraint],
) -> None:
    keys = [item.key for item in constraints]
    if len(keys) != len(set(keys)):
        raise ValueError("context constraints must have unique keys")


def _validated_product_ids(
    values: Collection[ProductId],
) -> tuple[ProductId, ...]:
    result = tuple(values)
    if len(result) > MAX_CONTEXT_PRODUCTS:
        raise ValueError("context product IDs exceed the bound")
    if any(type(value) is not int or value <= 0 for value in result):
        raise ValueError("context product IDs must be positive strict integers")
    if len(result) != len(set(result)):
        raise ValueError("context product IDs must be unique")
    return result


def _validate_limit(value: int, maximum: int, label: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")


@overload
def _aware(value: datetime) -> datetime: ...


@overload
def _aware(value: None) -> None: ...


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None and value.utcoffset() is not None:
        return value
    return value.replace(tzinfo=UTC)


__all__ = [
    "ContextConstraint",
    "HistoryDataError",
    "HistoryTurn",
    "MAX_CONTEXT_CONSTRAINTS",
    "MAX_CONTEXT_PRODUCTS",
    "MAX_CONTEXT_PREFERENCES",
    "MAX_CONTEXT_TURNS",
    "MAX_CONVERSATION_LIST",
    "MAX_HISTORY_TURNS",
    "ModelContext",
    "PreferenceSourceError",
    "V2HistoryService",
]
