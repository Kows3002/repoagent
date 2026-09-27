from threading import Lock

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.activity import record_activity
from app.auth import (
    get_current_session, get_owned_job, repository_for_job, require_csrf, token_for_job,
)
from app.database import get_db
from app.models import AuthSession, Job, utc_timestamp
from app.schemas import JobApprove, JobCreate, JobResponse
from app.git_push_service import GitPushError, commit_and_push
from app.worker import run_job

router = APIRouter(prefix="/jobs", tags=["Jobs"])
# Bound memory while serializing simultaneous approvals within one API process.
_approval_locks = tuple(Lock() for _ in range(64))


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
        db.flush()
        record_activity(db, current.user_id, "job_created", new_job.id)
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
    return (db.query(Job).filter(Job.user_id == current.user_id)
            .order_by(Job.id.desc()).limit(50).all())


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: int, db: Session = Depends(get_db), current: AuthSession = Depends(get_current_session)):
    return get_owned_job(job_id, current, db)


@router.post("/{job_id}/approve")
def approve_job(
    job_id: int, db: Session = Depends(get_db), current: AuthSession = Depends(require_csrf),
    approval: JobApprove | None = None,
):
    with _approval_locks[job_id % len(_approval_locks)]:
        job = get_owned_job(job_id, current, db)
        try:
            # PostgreSQL also serializes approvals across API processes. Refresh
            # after taking the lock so waiting requests see the saved push state.
            db.refresh(job, with_for_update=True)
            if job.pushed_at is not None:
                return {
                    "message": "Changes pushed successfully",
                    "job_id": job.id,
                    "commit_message": job.commit_message,
                    "pushed_at": job.pushed_at,
                }

            if job.status != "completed":
                raise HTTPException(status_code=409, detail="Wait for the code change to complete before approving.")

            if not job.diff or not job.diff.strip():
                raise HTTPException(status_code=409, detail="Nothing changed. The requested text already matches the repository.")

            if not job.workspace_path:
                raise HTTPException(status_code=400, detail="Workspace not found")

            # Permissions can change between generation and approval.
            token = token_for_job(job)
            result = commit_and_push(
                job.workspace_path,
                token,
                (approval or JobApprove()).commit_message,
            )
            job.pushed_at = utc_timestamp()
            job.commit_message = result.commit_message
            record_activity(db, current.user_id, "job_pushed", job.id)
            db.commit()

            return {
                "message": result.message,
                "job_id": job.id,
                "commit_message": job.commit_message,
                "pushed_at": job.pushed_at,
            }
        except HTTPException:
            db.rollback()
            raise
        except GitPushError as error:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(error)) from None
        except Exception:
            db.rollback()
            raise HTTPException(
                status_code=500,
                detail="Changes could not be pushed. Please try again.",
            ) from None
