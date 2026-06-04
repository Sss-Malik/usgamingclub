# app/main.py
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from app.api import health, operations
from app.config import get_settings
from app.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    get_logger(__name__).info("service_started", env=settings.env)
    try:
        yield
    finally:
        await app.state.arq.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Casino Game Service", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(operations.router)
    return app


app = create_app()
