import asyncio
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from config import config_manager
from database import init_db, seed_inbox_from_env
from filewatcher import start_filewatcher
from health_watcher import start_health_watcher
from routers import dashboard, setup, settings, logs, api, duplicates, review, diagnostics, tools

# Configure logging for Docker stdout
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


async def _supervise(name: str, coro):
    """Run a background task and make its death visible.

    Both watchers are fire-and-forget. Without this an exception in either one
    stops all processing while the web UI keeps answering normally — v2.32.6
    shipped exactly that: a NameError killed the filewatcher at startup, and
    nothing polled or processed for hours with no trace in the log table.
    """
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logging.getLogger("mediaassistant").exception("Background task %s died", name)
        try:
            from system_logger import log_error
            await log_error(name, f"Hintergrundprozess abgestürzt: {type(e).__name__}: {e}")
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await config_manager.seed_from_env()
    await seed_inbox_from_env()
    shutdown_event = asyncio.Event()
    watcher_task = asyncio.create_task(_supervise("filewatcher", start_filewatcher(shutdown_event)))
    health_task = asyncio.create_task(_supervise("health_watcher", start_health_watcher(shutdown_event)))
    yield
    shutdown_event.set()
    watcher_task.cancel()
    health_task.cancel()
    for t in (watcher_task, health_task):
        try:
            await t
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
app.include_router(diagnostics.router)
app.include_router(tools.router)
