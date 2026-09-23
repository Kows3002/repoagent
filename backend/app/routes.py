from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import (
    get_current_session, get_owned_job, repository_for_job, require_csrf, token_for_job,
)
from app.database import get_db
from app.models import AuthSession, Job
from app.schemas import JobApprove, JobCreate, JobResponse
from app.git_push_service import GitPushError, commit_and_push
from app.worker import run_job

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.post("", response_model=JobResponse, status_code=202)
@router.post("/", response_model=JobResponse, status_code=202, include_in_schema=False)
def create_job(
    job: JobCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db),
    current: AuthSession = Depends(require_csrf),
):
    repository = repository_for_job(job.repo_url, current)
    try:
        new_job = Job(
            repo_url=repository["clone_url"],
            user_id=current.user_id,
            task=job.task,
            status="queued"
        )

        db.add(new_job)
        db.commit()
        db.refresh(new_job)

        # The synchronous wrapper runs in Starlette's threadpool after the
        # response is sent, keeping status polling responsive during git/AI work.
        background_tasks.add_task(run_job, new_job.id)
        return new_job

    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="The job could not be created. Please try again.",
        ) from None


@router.get("/", response_model=list[JobResponse])
@router.get("", response_model=list[JobResponse], include_in_schema=False)
def get_jobs(db: Session = Depends(get_db), current: AuthSession = Depends(get_current_session)):
    return db.query(Job).filter(Job.user_id == current.user_id).all()


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: int, db: Session = Depends(get_db), current: AuthSession = Depends(get_current_session)):
    return get_owned_job(job_id, current, db)


@router.post("/{job_id}/approve")
def approve_job(
    job_id: int, db: Session = Depends(get_db), current: AuthSession = Depends(require_csrf),
    approval: JobApprove | None = None,
):
    job = get_owned_job(job_id, current, db)

    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Wait for the code change to complete before approving.")

    if not job.diff or not job.diff.strip():
        raise HTTPException(status_code=409, detail="Nothing changed. The requested text already matches the repository.")

    if not job.workspace_path:
        raise HTTPException(status_code=400, detail="Workspace not found")

    # Permissions can change between generation and approval.
    repository_for_job(job.repo_url, current)
    token = token_for_job(job)
    try:
        result = commit_and_push(
            job.workspace_path,
            token,
            (approval or JobApprove()).commit_message,
        )

        return {
            "message": result.message,
            "job_id": job.id,
            "commit_message": result.commit_message,
        }

    except GitPushError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Changes could not be pushed. Please try again.",
        ) from None
