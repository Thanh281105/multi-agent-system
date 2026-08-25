"""Declared benchmark failure injection shared by evaluation runners."""

from __future__ import annotations

from app.agents import AgentDispatcher
from app.contracts import AgentError, AgentMessage, AgentResult, TaskStatus
from app.evaluation.models import FailureInjection


class FailureInjectingDispatcher(AgentDispatcher):
    """Inject one declared agent failure without changing production agents."""

    def __init__(
        self,
        delegate: AgentDispatcher,
        injection: FailureInjection,
    ) -> None:
        self._delegate = delegate
        self._injection = injection

    async def dispatch(self, message: AgentMessage) -> AgentResult:
        if (
            message.target == self._injection.agent_id
            and message.action == self._injection.action
        ):
            return AgentResult(
                task_id=message.task_id,
                agent_id=self._injection.agent_id,
                status=TaskStatus.FAILED,
                errors=(
                    AgentError(
                        code=self._injection.error_code,
                        message="Agent lỗi có chủ đích trong benchmark offline.",
                        source=self._injection.agent_id,
                        retryable=True,
                    ),
                ),
            )
        return await self._delegate.dispatch(message)

    def agent_ids(self) -> tuple[str, ...]:
        return self._delegate.agent_ids()
