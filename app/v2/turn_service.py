"""Canonical v2 turn admission, attachment, query, and cancellation service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from app.contracts import AuthorizationContext
from app.db.v2_repository import V2Repository, canonical_turn_id
from app.shared.budget import ProviderBudgetContext
from app.v2.authorization import ResourceAuthorization
from app.v2.contracts import ChatRequest, TurnStatus
from app.v2.execution import (
    DurableReadTurnExecutor,
    DurableTurnOutcome,
    DurableTurnRequest,
)
from app.v2.planning import PlanningContext
from app.v2.progress import (
    ProgressCallback,
    TurnProgress,
    TurnProgressHub,
    TurnProgressPhase,
    emit_progress,
)


@dataclass(slots=True)
class _ActiveRun:
    task: asyncio.Task[DurableTurnOutcome]
    cancellation: asyncio.Event


class V2TurnService:
    """Make PostgreSQL claims authoritative while attaching same-process retries."""

    def __init__(
        self,
        executor: DurableReadTurnExecutor,
        *,
        progress_hub: TurnProgressHub | None = None,
        worker_id: str | None = None,
    ) -> None:
        resolved_worker = worker_id or f"worker_{uuid4().hex}"
        if not resolved_worker.strip() or len(resolved_worker) > 120:
            raise ValueError("worker_id must contain between 1 and 120 characters")
        self.executor = executor
        self.session_factory = executor.session_factory
        self.progress_hub = progress_hub or TurnProgressHub()
        self.worker_id = resolved_worker
        self._active: dict[str, _ActiveRun] = {}
        self._active_lock = asyncio.Lock()

    async def execute(
        self,
        request: ChatRequest,
        context: PlanningContext,
        *,
        provider_budget: ProviderBudgetContext | None = None,
        progress: ProgressCallback | None = None,
    ) -> DurableTurnOutcome:
        """Execute the SQL claimant or attach/replay without dispatching again."""

        turn_id = canonical_turn_id(request.conversation_id, request.client_turn_id)
        durable_request = DurableTurnRequest(
            conversation_id=request.conversation_id,
            turn_id=turn_id,
            client_turn_id=request.client_turn_id,
            message=request.message,
            lease_owner=f"{self.worker_id}:{uuid4().hex}",
        )
        callback = self._progress_callback(progress)
        admission = await self.executor.admit(
            durable_request,
            context,
            provider_budget=provider_budget,
            progress=callback,
        )
        if not admission.claimed:
            active = await self._active_run(turn_id)
            if active is None or active.task is asyncio.current_task():
                return admission.outcome
            try:
                outcome = await asyncio.shield(active.task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                outcome = self.query(
                    turn_id,
                    context.access,
                    provider_budget=provider_budget,
                )
            await self._emit_direct_terminal(progress, outcome)
            return outcome.model_copy(update={"reused": True})

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("v2 turn execution requires an asyncio task")
        cancellation = asyncio.Event()
        active_run = _ActiveRun(task=task, cancellation=cancellation)
        async with self._active_lock:
            self._active[turn_id] = active_run
        try:
            return await self.executor.run_admitted(
                admission,
                context,
                provider_budget=provider_budget,
                progress=callback,
                cancellation_probe=cancellation.is_set,
            )
        finally:
            async with self._active_lock:
                if self._active.get(turn_id) is active_run:
                    self._active.pop(turn_id, None)

    def query(
        self,
        turn_id: str,
        access: ResourceAuthorization,
        *,
        provider_budget: ProviderBudgetContext | None = None,
    ) -> DurableTurnOutcome:
        """Return the current owner-authorized durable state and ledger usage."""

        return self.executor.query(
            turn_id,
            access,
            provider_budget=provider_budget,
        )

    async def cancel(
        self,
        turn_id: str,
        access: ResourceAuthorization,
        *,
        provider_budget: ProviderBudgetContext | None = None,
    ) -> DurableTurnOutcome:
        """Persist cancellation first, then stop this process's claimed task."""

        with self.session_factory() as session:
            turn = V2Repository(session).cancel_turn(_authorization(access), turn_id)
            status = TurnStatus(str(turn.execution_state))
        outcome = self.query(turn_id, access, provider_budget=provider_budget)
        if status is TurnStatus.CANCELLED:
            active = await self._active_run(turn_id)
            if active is not None:
                active.cancellation.set()
                if active.task is not asyncio.current_task() and not active.task.done():
                    active.task.cancel()
            await self.progress_hub.publish(
                TurnProgress(
                    turn_id=turn_id,
                    phase=TurnProgressPhase.TERMINAL,
                    turn_status=TurnStatus.CANCELLED,
                )
            )
        return outcome

    async def _active_run(self, turn_id: str) -> _ActiveRun | None:
        async with self._active_lock:
            return self._active.get(turn_id)

    def _progress_callback(
        self,
        callback: ProgressCallback | None,
    ) -> ProgressCallback:
        async def publish(event: TurnProgress) -> None:
            await self.progress_hub.publish(event)
            await emit_progress(callback, event)

        return publish

    @staticmethod
    async def _emit_direct_terminal(
        callback: ProgressCallback | None,
        outcome: DurableTurnOutcome,
    ) -> None:
        if outcome.status not in {
            TurnStatus.COMPLETED,
            TurnStatus.FAILED,
            TurnStatus.CANCELLED,
            TurnStatus.INTERRUPTED,
        }:
            return
        await emit_progress(
            callback,
            TurnProgress(
                turn_id=outcome.turn_id,
                phase=TurnProgressPhase.TERMINAL,
                turn_status=outcome.status,
            ),
        )


__all__ = ["V2TurnService", "canonical_turn_id"]


def _authorization(access: ResourceAuthorization) -> AuthorizationContext:
    return AuthorizationContext(
        principal_id=access.binding.principal_id,
        tenant_id=access.binding.tenant_id,
        scopes=access.scopes,
    )
