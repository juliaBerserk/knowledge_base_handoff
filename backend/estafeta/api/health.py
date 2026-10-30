from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from estafeta.core.db import get_engine, ping

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    ok: bool
    service: str
    database: bool
    vector_extension: bool


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    try:
        database_ok, vector_ok = ping(get_engine())
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="База данных недоступна",
        ) from exc
    if not database_ok:
        raise HTTPException(status_code=503, detail="База данных недоступна")
    return HealthResponse(
        ok=True,
        service="estafeta",
        database=True,
        vector_extension=vector_ok,
    )
