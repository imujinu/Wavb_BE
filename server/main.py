from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes.audio import router as audio_router
from routes.rag import router as rag_router
from settings import get_settings


settings = get_settings()

app = FastAPI(title="Recordoc Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(audio_router)
app.include_router(rag_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
