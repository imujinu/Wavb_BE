import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes.audio import router as audio_router
from routes.auth import router as auth_router
from routes.oauth import router as oauth_router
from routes.rag import router as rag_router
from routes.realtime import router as realtime_router
from settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)


settings = get_settings()

app = FastAPI(title="Recordoc Backend", version="0.1.0")

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
app.include_router(oauth_router)
app.include_router(rag_router)
app.include_router(realtime_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
