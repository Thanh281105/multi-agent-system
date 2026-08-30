"""FastAPI composition root for the modular multi-agent platform."""

import logging

from fastapi import FastAPI

from app.api.chat import router as chat_router
from app.core.config import Settings, settings
from app.gateway import build_gateway_runtime
from app.gateway.frontend import install_frontend
from app.gateway.middleware import install_gateway_middleware
from app.gateway.operations import router as operations_router
from app.gateway.routes import router as gateway_router
from app.knowledge import KnowledgeStore

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def create_app(
    config: Settings = settings,
    *,
    knowledge_store: KnowledgeStore | None = None,
) -> FastAPI:
    """Build one production-guarded modular monolith application instance."""

    application = FastAPI(
        title="Vietnamese E-commerce Multi-Agent Platform",
        version="1.0.0",
        description=(
            "Authenticated, observable multi-agent recommendation platform "
            "using explicitly labeled sample data."
        ),
        docs_url=None if config.app_env == "production" else "/docs",
        redoc_url=None if config.app_env == "production" else "/redoc",
        openapi_url=None if config.app_env == "production" else "/openapi.json",
    )
    application.state.gateway_runtime = build_gateway_runtime(
        config,
        knowledge_store=knowledge_store,
    )
    install_gateway_middleware(application)
    application.include_router(operations_router)
    application.include_router(gateway_router)
    if config.legacy_chat_enabled:
        application.include_router(chat_router)
    install_frontend(application)
    return application


app = create_app()
