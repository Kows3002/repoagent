import traceback
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Job
from app.schemas import JobCreate, JobResponse
from app.git_push_service import commit_and_push
from app.worker import process_job

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.post("/", response_model=JobResponse)
async def create_job(job: JobCreate, db: Session = Depends(get_db)):
    try:
        new_job = Job(
            repo_url=job.repo_url,
            github_token=job.github_token,
            task=job.task,
            status="queued"
        )

        db.add(new_job)
        db.commit()
        db.refresh(new_job)

        # Process immediately
        await process_job(None, new_job.id)

        db.refresh(new_job)
        return new_job

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/", response_model=list[JobResponse])
def get_jobs(db: Session = Depends(get_db)):
    return db.query(Job).all()


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return job


@router.post("/{job_id}/approve")
def approve_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if not job.workspace_path:
        raise HTTPException(status_code=400, detail="Workspace not found")

    try:
        result = commit_and_push(
            job.workspace_path,
            job.github_token
        )

        return {
            "message": result,
            "job_id": job.id
        }

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))