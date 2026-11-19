from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from estafeta.api.dependencies import (
    Principal,
    get_db_session,
    require_knowledge_user,
    verify_csrf,
)
from estafeta.schemas.build_jobs import BuildJobListResponse, BuildJobResponse
from estafeta.services.build_jobs import (
    create_build_job,
    get_build_job,
    list_build_jobs,
)

router = APIRouter(prefix="/build-jobs", tags=["build-jobs"])


@router.post("", response_model=BuildJobResponse, status_code=status.HTTP_202_ACCEPTED)
def enqueue_build(
    request: Request,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> BuildJobResponse:
    verify_csrf(request, principal)
    try:
        job = create_build_job(session, user=principal.user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return BuildJobResponse.model_validate(job, from_attributes=True)


@router.get("", response_model=BuildJobListResponse)
def get_builds(
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> BuildJobListResponse:
    jobs = list_build_jobs(session, user=principal.user)
    return BuildJobListResponse(
        items=[BuildJobResponse.model_validate(job, from_attributes=True) for job in jobs]
    )


@router.get("/{job_id}", response_model=BuildJobResponse)
def get_build(
    job_id: UUID,
    principal: Principal = Depends(require_knowledge_user),
    session: Session = Depends(get_db_session),
) -> BuildJobResponse:
    job = get_build_job(session, user=principal.user, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задание сборки не найдено")
    return BuildJobResponse.model_validate(job, from_attributes=True)
