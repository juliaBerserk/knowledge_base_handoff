from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from estafeta.api.health import router as health_router
from estafeta.core.config import get_settings

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    description="Система сохранения и передачи корпоративных знаний",
    version="0.1.0",
)

origins = settings.cors_origin_list()
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(health_router, prefix="/api")
