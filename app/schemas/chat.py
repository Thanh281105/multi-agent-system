"""Pydantic contracts for the chat API and exposed tool metadata."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatRequest(BaseModel):
    """User message accepted by the chat endpoint."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1, max_length=2_000)
    session_id: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("message không được để trống")
        return cleaned

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("session_id không được để trống")
        return cleaned


class ToolCallInfo(BaseModel):
    """Safe, non-chain-of-thought information about one tool call."""

    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_summary: dict[str, Any] | None = None


class ChatResponse(BaseModel):
    """Grounded agent response returned by the chat endpoint."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    tool_calls: list[ToolCallInfo] = Field(default_factory=list)
    session_id: str
