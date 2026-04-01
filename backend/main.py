import asyncio
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from config import config_manager
from database import init_db, seed_inbox_from_env
from filewatcher import start_filewatcher
from routers import dashboard, setup, settings, logs, api, duplicates, review

# Configure logging for Docker stdout
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await config_manager.seed_from_env()
    await seed_inbox_from_env()
    shutdown_event = asyncio.Event()
    watcher_task = asyncio.create_task(start_filewatcher(shutdown_event))
    yield
    shutdown_event.set()
    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass


from version import VERSION
from auth import AuthMiddleware, AUTH_MODE, get_session_secret
from routers import auth_oidc

app = FastAPI(title="MediaAssistant", version=VERSION, lifespan=lifespan)

# Middleware order: last added = runs first
# 1) Auth middleware checks session/headers (runs second)
app.add_middleware(AuthMiddleware)
# 2) Session middleware provides request.session (runs first, needed by auth)
if AUTH_MODE == "oidc":
    from starlette.middleware.sessions import SessionMiddleware
    app.add_middleware(SessionMiddleware, secret_key=get_session_secret())

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(auth_oidc.router)
app.include_router(dashboard.router)
app.include_router(setup.router)
app.include_router(settings.router)
app.include_router(logs.router)
app.include_router(api.router)
app.include_router(duplicates.router)
app.include_router(review.router)
