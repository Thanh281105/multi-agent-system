"""Make two bounded, schema-only live calls without printing credentials/prompts."""

from __future__ import annotations

import asyncio

from app.core.config import settings
from app.orchestrator.model_schemas import (
    GroundedClaim,
    GroundedSynthesis,
    RoutingDecision,
)
from app.shared import ModelRuntimeError, OpenAIModelRuntime


async def smoke() -> int:
    key = (settings.openai_api_key or "").strip()
    if not key:
        print("LIVE_MODEL_STATUS=SKIPPED reason=missing_api_key")
        return 2
    runtime = OpenAIModelRuntime(
        key,
        timeout_seconds=settings.openai_request_timeout_seconds,
        max_retries=1,
        max_output_tokens=500,
        max_concurrency=2,
    )
    try:
        route = await runtime.generate_structured(
            stage="routing",
            agent_id="orchestrator",
            model=settings.openai_routing_model,
            instructions=(
                "Classify the Vietnamese commerce request using only the schema. "
                "Use a short reason code, not hidden reasoning."
            ),
            input_text="Tìm tai nghe dưới một triệu và ít bị phàn nàn.",
            schema=RoutingDecision,
            max_output_tokens=400,
            reasoning_effort="low",
        )
        synthesis = await runtime.generate_structured(
            stage="synthesis",
            agent_id="orchestrator",
            model=settings.openai_synthesis_model,
            instructions=(
                "Return one Vietnamese claim tied only to the supplied source ID."
            ),
            input_text=(
                "Fact: sản phẩm mẫu A có rating 4.8. "
                "Allowed source ID: smoke:sample."
            ),
            schema=GroundedSynthesis,
            max_output_tokens=400,
            reasoning_effort="low",
        )
    except ModelRuntimeError as exc:
        print(f"LIVE_MODEL_STATUS=FAILED code={exc.code}")
        return 1
    if not route.value.intent or not synthesis.value.claims:
        print("LIVE_MODEL_STATUS=FAILED code=empty_structured_output")
        return 1
    if not all(
        isinstance(claim, GroundedClaim) for claim in synthesis.value.claims
    ):
        print("LIVE_MODEL_STATUS=FAILED code=invalid_claim_contract")
        return 1
    total_tokens = route.metadata.total_tokens + synthesis.metadata.total_tokens
    print(
        "LIVE_MODEL_STATUS=SUCCESS "
        f"routing_model={route.metadata.model} "
        f"synthesis_model={synthesis.metadata.model} "
        f"total_tokens={total_tokens}"
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(smoke()))


if __name__ == "__main__":
    main()
