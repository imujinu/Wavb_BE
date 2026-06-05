import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from routes.audio import router as audio_router
from routes.auth import router as auth_router
from routes.files import router as files_router
from routes.oauth import router as oauth_router
from routes.rag import router as rag_router
from routes.realtime import router as realtime_router
from routes.work_items import router as work_items_router
from settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)


settings = get_settings()

app = FastAPI(title="Recordoc Backend", version="0.1.0")
Path(settings.upload_storage_dir).mkdir(parents=True, exist_ok=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    # allow_credentials=True,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(audio_router)
app.include_router(auth_router)
app.include_router(files_router)
app.include_router(oauth_router)
app.include_router(rag_router)
app.include_router(realtime_router)
app.include_router(work_items_router)
app.mount(
    settings.upload_public_path,
    StaticFiles(directory=settings.upload_storage_dir),
    name="uploads",
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
