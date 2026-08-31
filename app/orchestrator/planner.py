"""Build authorized execution plans from registry-owned intent manifests."""

from __future__ import annotations

import json
from uuid import uuid4

from app.contracts import ExecutionPlan, ExecutionStep
from app.orchestrator.model_schemas import PlanningDecision
from app.orchestrator.schemas import RoutedIntent
from app.registry import AgentRegistry, default_registry
from app.shared import (
    ModelRuntime,
    ModelRuntimeError,
    ModelRuntimeMode,
    ReasoningEffort,
    mark_model_call_fallback,
)


class ExecutionPlanner:
    """Compile a routed intent through its registered, bounded plan template."""

    def __init__(
        self,
        *,
        registry: AgentRegistry = default_registry,
        model_runtime: ModelRuntime | None = None,
        runtime_mode: ModelRuntimeMode = "off",
        model: str = "gpt-5.4-mini",
        reasoning_effort: ReasoningEffort = "low",
    ) -> None:
        self.registry = registry
        self.model_runtime = model_runtime
        self.runtime_mode = runtime_mode
        self.model = model
        self.reasoning_effort = reasoning_effort

    def build(self, routed: RoutedIntent) -> ExecutionPlan:
        manifest = self.registry.intent_manifest(routed.intent)
        if manifest is None:
            return self._plan("general.unsupported", ())
        return self._plan(
            routed.intent,
            manifest.build_steps(dict(routed.entities)),
        )

    async def build_async(self, routed: RoutedIntent) -> ExecutionPlan:
        """Ask the model for capabilities, then compile the registered DAG."""

        fallback = self.build(routed)
        manifest = self.registry.intent_manifest(routed.intent)
        expected = (
            manifest.expected_capabilities(dict(routed.entities))
            if manifest is not None
            else ()
        )
        if self.model_runtime is None or self.runtime_mode == "off" or not expected:
            return fallback

        try:
            result = await self.model_runtime.generate_structured(
                stage="planning",
                agent_id="orchestrator",
                model=self.model,
                instructions=(
                    "Bạn là planner cho trợ lý multi-agent sách trên snapshot lịch "
                    "sử Tiki Books. Chọn tập capability nhỏ nhất, đúng thứ tự phụ "
                    "thuộc để xử lý intent. Không suy rộng thành catalog hay thị "
                    "trường hiện tại. Chỉ dùng "
                    "capability trong schema; không tạo MCP tool, server, action, "
                    "agent hay dữ liệu đầu vào mới. Sao chép chính xác "
                    "authorized_capability_sequence theo đúng thứ tự, không thêm, "
                    "bớt hoặc sắp xếp lại. rationale là lý do ngắn, không phải "
                    "chuỗi suy luận."
                ),
                input_text=json.dumps(
                    {
                        "route": routed.model_dump(mode="json"),
                        "authorized_capability_sequence": list(expected),
                        "policy": {
                            "max_steps": 8,
                            "max_candidates": 5,
                            "tool_access": "agent_gateway_only",
                        },
                    },
                    ensure_ascii=False,
                ),
                schema=PlanningDecision,
                max_output_tokens=500,
                reasoning_effort=self.reasoning_effort,
            )
        except ModelRuntimeError as exc:
            if self.runtime_mode == "required":
                raise
            mark_model_call_fallback(exc.metadata, "deterministic_planning")
            return fallback

        if self.runtime_mode == "shadow":
            mark_model_call_fallback(result.metadata, "shadow_mode")
            return fallback

        proposed = tuple(result.value.capabilities)
        if proposed != expected:
            mark_model_call_fallback(result.metadata, "unauthorized_plan")
            if self.runtime_mode == "required":
                raise ValueError("model_plan_not_authorized")
            return fallback

        entities = dict(routed.entities)
        if routed.intent in {"product.rank", "multi.recommendation"}:
            entities["limit"] = result.value.candidate_limit
        compiled = self.build(routed.model_copy(update={"entities": entities}))
        if len(compiled.steps) > 8:
            mark_model_call_fallback(result.metadata, "plan_step_limit")
            if self.runtime_mode == "required":
                raise ValueError("model_plan_exceeds_step_limit")
            return fallback
        return compiled

    @staticmethod
    def _plan(
        intent: str,
        steps: tuple[ExecutionStep, ...],
    ) -> ExecutionPlan:
        return ExecutionPlan(
            plan_id=f"plan_{uuid4().hex}",
            intent=intent,
            steps=steps,
        )
