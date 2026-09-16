import os
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from arq import create_pool
from arq.connections import RedisSettings

from app.database import get_db
from app.models import Job
from app.schemas import JobCreate, JobResponse
from app.git_push_service import commit_and_push

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.post("/", response_model=JobResponse)
async def create_job(job: JobCreate, db: Session = Depends(get_db)):

    new_job = Job(
        repo_url=job.repo_url,
        task=job.task,
        status="queued"
    )

    db.add(new_job)
    db.commit()
    db.refresh(new_job)

    # Render Redis connection
    redis_url = os.getenv("REDIS_URL")

    if not redis_url:
        raise HTTPException(
            status_code=500,
            detail="REDIS_URL environment variable not configured"
        )

    url = urlparse(redis_url)

    redis = await create_pool(
        RedisSettings(
            host=url.hostname,
            port=url.port,
            password=url.password,
        )
    )

    await redis.enqueue_job("process_job", new_job.id)

    return new_job


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

    result = commit_and_push(job.workspace_path)

    return {
        "message": result,
        "job_id": job.id
    }