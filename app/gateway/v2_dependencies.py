"""Dependencies and safe exception translation for the JSON v2 adapter."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Request
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError

from app.db.v2_repository import TurnPayloadConflictError, TurnStateConflictError
from app.gateway.dependencies import get_runtime
from app.gateway.errors import GatewayAPIError
from app.shared.budget import BudgetError
from app.v2.actions import (
    ActionConflictError,
    ActionIdempotencyConflictError,
    ActionInProgressError,
    ActionServiceError,
    ActionStateConflictError,
)
from app.v2.authorization import AuthorizationDeniedError, ResourceNotFoundError
from app.v2.execution import DurableExecutionError
from app.v2.history import (
    HistoryConversationBusyError,
    HistoryDataError,
    PreferenceSourceError,
)
from app.v2.runtime import V2RuntimeConfigurationError, V2RuntimeFactory


def get_v2_runtime_factory(request: Request) -> V2RuntimeFactory:
    """Return the lazy v2 factory owned by the authenticated gateway runtime."""

    return get_runtime(request).v2_runtime_factory


@contextmanager
def translate_v2_errors() -> Iterator[None]:
    """Map internal failures to stable, non-sensitive HTTP errors."""

    try:
        yield
    except ResourceNotFoundError:
        raise _api_error(
            404,
            "v2.resource_not_found",
            "Không tìm thấy tài nguyên.",
        ) from None
    except AuthorizationDeniedError:
        raise _api_error(
            403,
            "v2.forbidden",
            "Không có quyền thực hiện yêu cầu này.",
        ) from None
    except TurnPayloadConflictError:
        raise _api_error(
            409,
            "v2.turn_payload_conflict",
            "Yêu cầu xung đột với trạng thái hiện tại.",
        ) from None
    except TurnStateConflictError:
        raise _api_error(
            409,
            "v2.turn_state_conflict",
            "Lượt hội thoại xung đột với trạng thái hiện tại.",
        ) from None
    except ActionIdempotencyConflictError:
        raise _api_error(
            409,
            "v2.idempotency_conflict",
            "Khóa idempotency đã được dùng cho yêu cầu khác.",
        ) from None
    except ActionInProgressError:
        raise _api_error(
            409,
            "v2.action_in_progress",
            "Thao tác đang được xử lý.",
            retryable=True,
        ) from None
    except ActionStateConflictError:
        raise _api_error(
            409,
            "v2.proposal_state_conflict",
            "Đề xuất xung đột với trạng thái hiện tại.",
        ) from None
    except ActionConflictError:
        raise _api_error(
            409,
            "v2.action_conflict",
            "Thao tác xung đột với trạng thái hiện tại.",
        ) from None
    except PreferenceSourceError:
        raise _api_error(
            409,
            "v2.preference_source_conflict",
            "Lượt nguồn của sở thích không hợp lệ.",
        ) from None
    except HistoryConversationBusyError:
        raise _api_error(
            409,
            "v2.conversation_busy",
            "Hội thoại đang được xử lý.",
            retryable=True,
        ) from None
    except (OperationalError, InterfaceError, TimeoutError):
        raise _api_error(
            503,
            "v2.database_unavailable",
            "Dịch vụ dữ liệu tạm thời không khả dụng.",
            retryable=True,
        ) from None
    except V2RuntimeConfigurationError:
        raise _api_error(
            503,
            "v2.runtime_unavailable",
            "Dịch vụ v2 tạm thời không khả dụng.",
            retryable=True,
        ) from None
    except BudgetError:
        raise _api_error(
            503,
            "v2.budget_service_unavailable",
            "Dịch vụ ngân sách tạm thời không khả dụng.",
            retryable=True,
        ) from None
    except ActionServiceError:
        raise _api_error(
            503,
            "v2.action_service_unavailable",
            "Dịch vụ thao tác tạm thời không khả dụng.",
            retryable=True,
        ) from None
    except DurableExecutionError as exc:
        if exc.retryable:
            raise _api_error(
                503,
                "v2.runtime_unavailable",
                "Dịch vụ v2 tạm thời không khả dụng.",
                retryable=True,
            ) from None
        raise _api_error(
            500,
            "v2.durable_data_invalid",
            "Dữ liệu bền vững không hợp lệ.",
        ) from None
    except HistoryDataError:
        raise _api_error(
            500,
            "v2.durable_data_invalid",
            "Dữ liệu bền vững không hợp lệ.",
        ) from None


def _api_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> GatewayAPIError:
    return GatewayAPIError(
        status_code=status_code,
        code=code,
        message=message,
        retryable=retryable,
    )


__all__ = ["get_v2_runtime_factory", "translate_v2_errors"]
