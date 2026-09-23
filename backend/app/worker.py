import asyncio
from pathlib import Path

from app.database import SessionLocal
from app.models import Job

from app.git_service import GitCloneError, clone_repository
from app.analyzer import (
    detect_project_type,
    find_relevant_files,
    read_files,
)
from app.ollama_service import analyze_code
from app.code_generator import generate_patch
from app.patch_service import apply_patch
from app.git_diff import get_diff
from app.auth import token_for_job
from app.preview_service import create_snapshot


def run_job(job_id: int):
    """Run the blocking git/AI pipeline in a background thread."""
    asyncio.run(process_job(None, job_id))


def _friendly_error(error: Exception) -> str:
    if isinstance(error, GitCloneError):
        return str(error)
    message = str(error).lower()
    if "couldn't locate the file" in message:
        return "AI couldn't locate the file. Include the exact file path in your task."
    if "repository not found" in message or "could not read username" in message:
        return "Repository not found. Verify the GitHub repository URL and repository access."
    if "authentication failed" in message or "bad credentials" in message:
        return "GitHub authorization failed. Sign in with GitHub again and check repository access."
    return "The code change could not be generated. Please check your task and try again."


async def process_job(ctx, job_id: int):
    db = SessionLocal()

    try:
        job = db.query(Job).filter(Job.id == job_id).first()

        if not job:
            return

        job.status = "analyzing"
        db.commit()

        # Clone repository
        print(f"\nCloning repository for Job {job_id}", flush=True)

        repo_folder = clone_repository(
            job.workspace_id,
            job.repo_url,
            token_for_job(job),
        )

        # Save workspace for approval & git push
        job.workspace_path = repo_folder
        db.commit()

        print(f"Repository cloned to {repo_folder}", flush=True)

        # Capture immutable original source before any model-generated changes.
        # Preview problems must never discard an otherwise reviewable code diff.
        try:
            create_snapshot(repo_folder, job.workspace_id, "before")
        except Exception:
            print(f"Preview snapshot unavailable for Job {job_id}", flush=True)

        # Analyze repository
        project = detect_project_type(repo_folder)
        files = find_relevant_files(repo_folder, job.task)
        if not files:
            raise ValueError("AI couldn't locate the file")
        contents = read_files(files)

        print(f"\nProject Type: {project}", flush=True)
        print("\nRelevant Files:", flush=True)

        for f in files:
            print(f, flush=True)

        # AI Analysis
        print("\nCalling Groq...", flush=True)

        analysis = analyze_code(
            job.task,
            project,
            contents
        )

        job.ai_result = analysis
        job.status = "generating"
        db.commit()

        print("\n===== AI ANALYSIS =====", flush=True)
        print(analysis, flush=True)

        # Generate patch & diff
        if files:
            target = files[0]

            # Linux + Windows compatible relative path
            relative_path = Path(target).relative_to(
                Path(repo_folder)
            ).as_posix()

            patch = generate_patch(
                job.task,
                relative_path,
                contents[Path(target).name]
            )

            # The generated patch may only target the file selected for this job.
            if not isinstance(patch, dict) or patch.get("filename") != relative_path:
                raise ValueError("The generated patch did not match the requested file")
            apply_patch(repo_folder, patch)
            try:
                create_snapshot(repo_folder, job.workspace_id, "after")
            except Exception:
                print(f"Updated preview snapshot unavailable for Job {job_id}", flush=True)

            diff = get_diff(repo_folder)

            job.diff = diff
            db.commit()

            print("\n===== GIT DIFF =====", flush=True)
            print(diff, flush=True)

        job.status = "completed"
        db.commit()

        print(f"\nCompleted Job {job_id}", flush=True)

    except Exception as e:
        message = _friendly_error(e)
        print(f"Job {job_id} failed: {message}", flush=True)

        if "job" in locals() and job:
            db.rollback()
            job.status = "failed"
            job.ai_result = message
            db.commit()

    finally:
        db.close()
