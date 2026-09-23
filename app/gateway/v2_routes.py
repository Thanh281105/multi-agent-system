"""Authenticated JSON transport for the durable version 2 services."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.db.v2_repository import V2Repository, canonical_turn_id
from app.gateway.dependencies import PrincipalContext, authorize_request
from app.gateway.schemas import GatewayErrorResponse
from app.gateway.v2_dependencies import (
    get_v2_runtime_factory,
    translate_v2_errors,
)
from app.gateway.v2_stream import build_v2_streaming_response
from app.shared.budget import ProviderBudgetContext
from app.v2.authorization import DEMO_STORE_ID, allowed_modes
from app.v2.contracts import (
    IDENTIFIER_PATTERN,
    ActionConfirmRequest,
    ActionDecisionResponse,
    ActionExecutionResponse,
    ActionReadResponse,
    ActionRejectRequest,
    ChatRequest,
    ChatResponse,
    ConversationCreateRequest,
    ConversationCreateResponse,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationMode,
    ConversationSummary,
    HistoryTurn,
    MeResponse,
    PreferenceDeleteRequest,
    PreferenceListResponse,
    PreferencePutRequest,
    PreferenceRecord,
    TurnResponse,
    TurnStatus,
    TurnSummary,
)
from app.v2.execution import DurableTurnOutcome
from app.v2.history import V2HistoryService
from app.v2.runtime import ResolvedV2Runtime

_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    code: {"model": GatewayErrorResponse}
    for code in (401, 403, 404, 409, 422, 500, 503)
}
router = APIRouter(
    prefix="/api/v2",
    tags=["chat-v2"],
    responses=_ERROR_RESPONSES,
)

Principal = Annotated[PrincipalContext, Depends(authorize_request)]
ModeQuery = Annotated[ConversationMode, Query()]
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
        pattern=r"^[!-~]+$",
    ),
]
StablePath = Annotated[str, Path(pattern=IDENTIFIER_PATTERN)]


@router.get("/me", response_model=MeResponse)
async def me(principal: Principal) -> MeResponse:
    """Return only identity and capabilities derived from trusted gateway policy."""

    return MeResponse(
        principal_id=principal.principal_id,
        tenant_id=principal.authorization.tenant_id,
        allowed_modes=allowed_modes(principal.authorization),
        store_id=DEMO_STORE_ID,
    )


@router.post(
    "/conversations",
    response_model=ConversationCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    payload: ConversationCreateRequest,
    request: Request,
    principal: Principal,
) -> ConversationCreateResponse:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        conversation = await asyncio.to_thread(
            factory.create_conversation,
            principal.authorization,
            mode=payload.mode,
        )
    return ConversationCreateResponse(conversation=conversation)


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(
    request: Request,
    principal: Principal,
    mode: ModeQuery,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> ConversationListResponse:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        runtime = await asyncio.to_thread(
            factory.resolve_for_request,
            principal.authorization,
            mode=mode,
        )
        conversations = await asyncio.to_thread(
            _list_conversations,
            runtime,
            principal,
            mode,
            limit,
        )
    return ConversationListResponse(conversations=conversations)


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
)
async def get_conversation(
    conversation_id: StablePath,
    request: Request,
    principal: Principal,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> ConversationDetailResponse:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        runtime = await asyncio.to_thread(
            factory.resolve_for_conversation,
            principal.authorization,
            conversation_id,
        )
        conversation = await asyncio.to_thread(
            factory.conversation_summary,
            principal.authorization,
            conversation_id,
        )
        turns = await asyncio.to_thread(
            _list_turns,
            runtime,
            principal,
            conversation_id,
            limit,
        )
    return ConversationDetailResponse(conversation=conversation, turns=turns)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation(
    conversation_id: StablePath,
    request: Request,
    principal: Principal,
) -> Response:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        runtime = await asyncio.to_thread(
            factory.resolve_for_conversation,
            principal.authorization,
            conversation_id,
            write=True,
        )
        await asyncio.to_thread(
            _delete_conversation,
            runtime,
            principal,
            conversation_id,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={status.HTTP_202_ACCEPTED: {"model": ChatResponse}},
)
async def chat(
    payload: ChatRequest,
    request: Request,
    response: Response,
    principal: Principal,
) -> ChatResponse:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        runtime = await asyncio.to_thread(
            factory.resolve_for_conversation,
            principal.authorization,
            payload.conversation_id,
        )
        budget = await asyncio.to_thread(_provider_budget, runtime, payload)
        outcome = await runtime.services.turn_service.execute(
            payload,
            runtime.planning_context,
            provider_budget=budget,
        )
        result = await asyncio.to_thread(
            _turn_response,
            runtime,
            principal,
            outcome,
            request.state.request_id,
            request.state.trace_id,
        )
    if outcome.reused and outcome.status in {TurnStatus.PENDING, TurnStatus.RUNNING}:
        response.status_code = status.HTTP_202_ACCEPTED
    return ChatResponse.model_validate(result.model_dump(mode="python"))


@router.post("/chat/stream", response_class=StreamingResponse)
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    principal: Principal,
) -> StreamingResponse:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        runtime = await asyncio.to_thread(
            factory.resolve_for_conversation,
            principal.authorization,
            payload.conversation_id,
        )
        budget = await asyncio.to_thread(_provider_budget, runtime, payload)
        return build_v2_streaming_response(
            request=request,
            payload=payload,
            resolved=runtime,
            provider_budget=budget,
        )


@router.get("/turns/{turn_id}", response_model=TurnResponse)
async def get_turn(
    turn_id: StablePath,
    request: Request,
    principal: Principal,
) -> TurnResponse:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        runtime = await asyncio.to_thread(
            factory.resolve_for_turn,
            principal.authorization,
            turn_id,
        )
        outcome = await asyncio.to_thread(
            runtime.services.turn_service.query,
            turn_id,
            runtime.access,
        )
        return await asyncio.to_thread(
            _turn_response,
            runtime,
            principal,
            outcome,
            request.state.request_id,
            request.state.trace_id,
        )


@router.post("/turns/{turn_id}/cancel", response_model=TurnResponse)
async def cancel_turn(
    turn_id: StablePath,
    request: Request,
    principal: Principal,
) -> TurnResponse:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        runtime = await asyncio.to_thread(
            factory.resolve_for_turn,
            principal.authorization,
            turn_id,
        )
        outcome = await runtime.services.turn_service.cancel(turn_id, runtime.access)
        return await asyncio.to_thread(
            _turn_response,
            runtime,
            principal,
            outcome,
            request.state.request_id,
            request.state.trace_id,
        )


@router.get("/actions/{action_id}", response_model=ActionReadResponse)
async def get_action(
    action_id: StablePath,
    request: Request,
    principal: Principal,
) -> ActionReadResponse:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        runtime = await asyncio.to_thread(
            factory.resolve_for_action,
            principal.authorization,
            action_id,
        )
        stored = await asyncio.to_thread(
            runtime.services.action_service.read_action_result,
            principal.authorization,
            action_id=action_id,
        )
        return ActionReadResponse.from_persistence(
            action=stored.card,
            persisted_result=stored.result,
        )


@router.post(
    "/actions/{action_id}/confirm",
    response_model=ActionExecutionResponse,
)
async def confirm_action(
    action_id: StablePath,
    payload: ActionConfirmRequest,
    request: Request,
    principal: Principal,
    idempotency_key: IdempotencyKey,
) -> ActionExecutionResponse:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        runtime = await asyncio.to_thread(
            factory.resolve_for_action,
            principal.authorization,
            action_id,
            write=True,
        )
        result = await asyncio.to_thread(
            runtime.services.action_service.confirm_action,
            principal.authorization,
            action_id=action_id,
            request=payload,
            idempotency_key=idempotency_key,
        )
        return ActionExecutionResponse.model_validate(result.model_dump(mode="python"))


@router.post(
    "/actions/{action_id}/reject",
    response_model=ActionDecisionResponse,
)
async def reject_action(
    action_id: StablePath,
    payload: ActionRejectRequest,
    request: Request,
    principal: Principal,
) -> ActionDecisionResponse:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        runtime = await asyncio.to_thread(
            factory.resolve_for_action,
            principal.authorization,
            action_id,
            write=True,
        )
        return await asyncio.to_thread(
            runtime.services.action_service.reject_action,
            principal.authorization,
            action_id=action_id,
            request=payload,
        )


@router.get("/memory", response_model=PreferenceListResponse)
async def get_memory(
    request: Request,
    principal: Principal,
    mode: ModeQuery,
) -> PreferenceListResponse:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        runtime = await asyncio.to_thread(
            factory.resolve_for_request,
            principal.authorization,
            mode=mode,
        )
        preferences = await asyncio.to_thread(
            _list_preferences,
            runtime,
            principal,
            mode,
        )
    return PreferenceListResponse(preferences=preferences)


@router.put("/memory", response_model=PreferenceRecord)
async def put_memory(
    payload: PreferencePutRequest,
    request: Request,
    principal: Principal,
) -> PreferenceRecord:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        runtime = await asyncio.to_thread(
            factory.resolve_for_turn,
            principal.authorization,
            payload.source_turn_id,
            write=True,
        )
        return await asyncio.to_thread(
            _put_preference,
            runtime,
            principal,
            payload,
        )


@router.delete("/memory", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    payload: PreferenceDeleteRequest,
    request: Request,
    principal: Principal,
    mode: ModeQuery,
) -> Response:
    factory = get_v2_runtime_factory(request)
    with translate_v2_errors():
        runtime = await asyncio.to_thread(
            factory.resolve_for_request,
            principal.authorization,
            mode=mode,
        )
        await asyncio.to_thread(
            _delete_preference,
            runtime,
            principal,
            payload,
            mode,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _provider_budget(
    runtime: ResolvedV2Runtime,
    payload: ChatRequest,
) -> ProviderBudgetContext:
    turn_id = canonical_turn_id(payload.conversation_id, payload.client_turn_id)
    runtime.services.budget_ledger.create_scope(
        scope_id=turn_id,
        account_id=runtime.services.budget_account_id,
        purpose="chat",
    )
    return ProviderBudgetContext(
        ledger=runtime.services.budget_ledger,
        scope_id=turn_id,
        purpose="chat",
    )


def _list_conversations(
    runtime: ResolvedV2Runtime,
    principal: PrincipalContext,
    mode: ConversationMode,
    limit: int,
) -> tuple[ConversationSummary, ...]:
    with runtime.services.session_factory() as session:
        return V2HistoryService(session).list_conversations(
            principal.authorization,
            mode=mode,
            limit=limit,
        )


def _list_turns(
    runtime: ResolvedV2Runtime,
    principal: PrincipalContext,
    conversation_id: str,
    limit: int,
) -> tuple[HistoryTurn, ...]:
    with runtime.services.session_factory() as session:
        return V2HistoryService(session).list_turns(
            principal.authorization,
            conversation_id,
            limit=limit,
        )


def _delete_conversation(
    runtime: ResolvedV2Runtime,
    principal: PrincipalContext,
    conversation_id: str,
) -> None:
    with runtime.services.session_factory() as session:
        V2HistoryService(session).delete_conversation(
            principal.authorization,
            conversation_id,
        )


def _list_preferences(
    runtime: ResolvedV2Runtime,
    principal: PrincipalContext,
    mode: ConversationMode,
) -> tuple[PreferenceRecord, ...]:
    with runtime.services.session_factory() as session:
        return V2HistoryService(session).list_preferences(
            principal.authorization,
            mode=mode,
        )


def _put_preference(
    runtime: ResolvedV2Runtime,
    principal: PrincipalContext,
    payload: PreferencePutRequest,
) -> PreferenceRecord:
    with runtime.services.session_factory() as session:
        return V2HistoryService(session).put_preference(
            principal.authorization,
            payload,
        )


def _delete_preference(
    runtime: ResolvedV2Runtime,
    principal: PrincipalContext,
    payload: PreferenceDeleteRequest,
    mode: ConversationMode,
) -> None:
    with runtime.services.session_factory() as session:
        V2HistoryService(session).delete_preference(
            principal.authorization,
            payload,
            mode=mode,
        )


def _turn_response(
    runtime: ResolvedV2Runtime,
    principal: PrincipalContext,
    outcome: DurableTurnOutcome,
    request_id: str,
    trace_id: str,
) -> TurnResponse:
    with runtime.services.session_factory() as session:
        row = V2Repository(session).get_turn(principal.authorization, outcome.turn_id)
    row_status = TurnStatus(row.execution_state)
    summary = TurnSummary(
        turn_id=row.id,
        client_turn_id=row.client_turn_id,
        status=outcome.status,
        outcome=outcome.outcome,
        created_at=_aware(row.created_at),
        completed_at=(
            _optional_aware(row.completed_at) if row_status is outcome.status else None
        ),
    )
    return TurnResponse(
        conversation_id=row.conversation_id,
        turn=summary,
        request_id=request_id,
        trace_id=trace_id,
        result=outcome.result,
        error=outcome.error,
        usage=outcome.usage,
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _optional_aware(value: datetime | None) -> datetime | None:
    return _aware(value) if value is not None else None


__all__ = ["router"]
