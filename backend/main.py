import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from config import config_manager
from database import init_db
from filewatcher import start_filewatcher
from routers import dashboard, setup, settings, logs, api, duplicates


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await config_manager.seed_from_env()
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

app = FastAPI(title="MediaAssistant", version=VERSION, lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(dashboard.router)
app.include_router(setup.router)
app.include_router(settings.router)
app.include_router(logs.router)
app.include_router(api.router)
app.include_router(duplicates.router)
