from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from estafeta.api.audit import router as audit_router
from estafeta.api.auth import router as auth_router
from estafeta.api.build import router as build_router
from estafeta.api.health import router as health_router
from estafeta.api.materials import router as materials_router
from estafeta.api.publication import router as publication_router
from estafeta.api.publication import settings_router
from estafeta.api.rag import router as rag_router
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
app.include_router(auth_router, prefix="/api")
app.include_router(audit_router, prefix="/api")
app.include_router(materials_router, prefix="/api")
app.include_router(publication_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(build_router, prefix="/api")
app.include_router(rag_router, prefix="/api")
