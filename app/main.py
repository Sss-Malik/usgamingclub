# app/main.py
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api import automation, health
from app.config import get_settings, require_runtime_settings
from app.db.engine import get_sessionmaker
from app.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    require_runtime_settings(settings)
    app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    app.state.session_factory = get_sessionmaker()
    get_logger(__name__).info("service_started", env=settings.env)
    try:
        yield
    finally:
        await app.state.arq.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Casino Game Service", lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(automation.router)
    return app


def register_exception_handlers(app: FastAPI) -> None:
    # The automation endpoints validate the body manually (raw bytes are needed for the
    # HMAC check first), so a bad body raises pydantic.ValidationError rather than
    # FastAPI's RequestValidationError. Map it to 422 to mirror normal body validation.
    @app.exception_handler(ValidationError)
    async def _on_validation_error(_request: Request, exc: ValidationError) -> JSONResponse:
        # include_context=False drops each error's `ctx`, which for a model_validator-raised
        # ValueError holds the raw exception object (not JSON-serializable). Without this a custom
        # validator would 500 instead of 422. The human-readable `msg` is preserved.
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors(include_url=False, include_context=False)},
        )


app = create_app()
