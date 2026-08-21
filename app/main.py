"""FastAPI application entry point."""

import logging

from fastapi import FastAPI

from app.api.chat import router as chat_router
from app.core.config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def create_app() -> FastAPI:
    """Build the small Phase 1 FastAPI application."""

    application = FastAPI(
        title="Vietnamese E-commerce Agent",
        version="0.1.0",
        description="Single-agent OpenAI Responses API proof of concept.",
    )
    application.include_router(chat_router)
    return application


app = create_app()
