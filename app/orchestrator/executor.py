"""Dependency-aware, concurrent in-process A2A plan execution."""

from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Any

from app.agents import AgentDispatcher
from app.contracts import (
    AgentError,
    AgentMessage,
    AgentResult,
    ExecutionPlan,
    ExecutionStep,
    TaskStatus,
)
from app.orchestrator.progress import (
    OrchestrationProgress,
    ProgressCallback,
    emit_progress,
)
from app.registry import AgentRegistry, default_registry
from app.shared import ExecutionContext, Telemetry


class PlanExecutor:
    """Execute ready DAG steps concurrently and bind upstream product IDs."""

    def __init__(
        self,
        dispatcher: AgentDispatcher,
        telemetry: Telemetry,
        *,
        registry: AgentRegistry = default_registry,
    ) -> None:
        self.dispatcher = dispatcher
        self.telemetry = telemetry
        self.registry = registry

    async def execute(
        self,
        plan: ExecutionPlan,
        context: ExecutionContext,
        progress: ProgressCallback | None = None,
    ) -> tuple[AgentResult, ...]:
        pending = {step.step_id: step for step in plan.steps}
        results: dict[str, AgentResult] = {}
        while pending:
            ready = [
                step
                for step in pending.values()
                if set(step.depends_on).issubset(results)
            ]
            if not ready:
                raise RuntimeError(
                    "execution plan contains an unresolved dependency cycle"
                )
            completed = await asyncio.gather(
                *(
                    self._execute_step(step, results, context, progress)
                    for step in ready
                )
            )
            for step, result in zip(ready, completed, strict=True):
                results[step.step_id] = result
                del pending[step.step_id]
        return tuple(results[step.step_id] for step in plan.steps)

    async def _execute_step(
        self,
        step: ExecutionStep,
        completed: dict[str, AgentResult],
        context: ExecutionContext,
        progress: ProgressCallback | None,
    ) -> AgentResult:
        await emit_progress(
            progress,
            OrchestrationProgress(
                phase="agent.started",
                message=f"{step.agent_id} đang xử lý tác vụ.",
                step_id=step.step_id,
                agent_id=step.agent_id,
                status=TaskStatus.RUNNING,
            ),
        )
        step_context = context.child(agent_id=step.agent_id)
        payload, binding_error = self._resolve_bindings(step, completed)
        if binding_error:
            result = AgentResult(
                task_id=step_context.task_id,
                agent_id=step.agent_id,
                status=TaskStatus.FAILED,
                errors=(binding_error,),
            )
            self.telemetry.record(
                step_context,
                component="orchestrator",
                operation="dependency_binding",
                outcome="failed",
                duration_ms=0,
                attributes={
                    "target_agent": step.agent_id,
                    "agent_version": self.registry.get(step.agent_id).version,
                },
            )
            await self._emit_completion(progress, step, result)
            return result

        message = AgentMessage.model_validate(
            {
                "task_id": step_context.task_id,
                "session_id": context.session_id,
                "request_id": context.request_id,
                "trace_id": context.trace_id,
                "authorization": context.authorization.model_dump(mode="json"),
                "source": "orchestrator",
                "target": step.agent_id,
                "action": step.action,
                "input": payload,
            }
        )
        started_at = perf_counter()
        try:
            result = await self.dispatcher.dispatch(message)
        except Exception:
            result = AgentResult(
                task_id=step_context.task_id,
                agent_id=step.agent_id,
                status=TaskStatus.FAILED,
                errors=(
                    AgentError(
                        code="orchestrator.agent_unavailable",
                        message="Domain agent không thể hoàn tất tác vụ.",
                        source="orchestrator",
                        retryable=True,
                    ),
                ),
            )
        self.telemetry.record(
            step_context,
            component=step.agent_id,
            operation=step.action,
            outcome=result.status.value,
            duration_ms=(perf_counter() - started_at) * 1_000,
            attributes={"agent_version": self.registry.get(step.agent_id).version},
        )
        await self._emit_completion(progress, step, result)
        return result

    @staticmethod
    async def _emit_completion(
        progress: ProgressCallback | None,
        step: ExecutionStep,
        result: AgentResult,
    ) -> None:
        await emit_progress(
            progress,
            OrchestrationProgress(
                phase="agent.completed",
                message=(
                    f"{step.agent_id} đã hoàn tất với trạng thái {result.status.value}."
                ),
                step_id=step.step_id,
                agent_id=step.agent_id,
                status=result.status,
            ),
        )

    def _resolve_bindings(
        self,
        step: ExecutionStep,
        completed: dict[str, AgentResult],
    ) -> tuple[dict[str, Any], AgentError | None]:
        payload = dict(step.input)
        product_source = payload.pop("product_id_from", None)
        if isinstance(product_source, str):
            source_result = completed.get(product_source)
            bound_product_ids = (
                self._product_ids(source_result) if source_result else []
            )
            if not bound_product_ids:
                return payload, self._binding_error(product_source)
            payload["product_id"] = bound_product_ids[0]

        product_sources = payload.pop("product_ids_from", None)
        if isinstance(product_sources, list):
            collected_product_ids: list[int] = []
            for source in product_sources:
                source_result = completed.get(str(source))
                ids = self._product_ids(source_result) if source_result else []
                if not ids:
                    return payload, self._binding_error(str(source))
                collected_product_ids.append(ids[0])
            payload["product_ids"] = collected_product_ids

        all_products_source = payload.pop("product_ids_all_from", None)
        if isinstance(all_products_source, str):
            source_result = completed.get(all_products_source)
            bound_product_ids = (
                self._product_ids(source_result) if source_result else []
            )
            if not bound_product_ids:
                return payload, self._binding_error(all_products_source)
            payload["product_ids"] = bound_product_ids[:5]
        return payload, None

    @staticmethod
    def _product_ids(result: AgentResult | None) -> list[int]:
        if result is None or result.status == TaskStatus.FAILED:
            return []
        candidates: object = result.data.get("products")
        if not isinstance(candidates, list):
            ranking = result.data.get("ranking")
            if isinstance(ranking, dict):
                candidates = ranking.get("products")
        if not isinstance(candidates, list):
            search = result.data.get("search")
            if isinstance(search, dict):
                candidates = search.get("products")
        if not isinstance(candidates, list):
            retrieval = result.data.get("retrieval")
            if isinstance(retrieval, dict):
                product = retrieval.get("product")
                if isinstance(product, dict) and isinstance(product.get("id"), int):
                    return [int(product["id"])]
            return []
        return [
            int(product["id"])
            for product in candidates
            if isinstance(product, dict) and isinstance(product.get("id"), int)
        ]

    @staticmethod
    def _binding_error(source: str) -> AgentError:
        return AgentError(
            code="orchestrator.missing_dependency_data",
            message=f"Không có product_id từ bước phụ thuộc {source}.",
            source="orchestrator",
        )
