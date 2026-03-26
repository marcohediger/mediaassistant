from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from database import init_db
from routers import dashboard, setup, settings, logs, api


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="MediaAssistant", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(dashboard.router)
app.include_router(setup.router)
app.include_router(settings.router)
app.include_router(logs.router)
app.include_router(api.router)
