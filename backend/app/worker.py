import asyncio
import traceback
from pathlib import Path

from app.database import SessionLocal
from app.models import Job

from app.git_service import clone_repository
from app.analyzer import (
    detect_project_type,
    find_relevant_files,
    read_files,
)
from app.ollama_service import analyze_code
from app.code_generator import generate_patch
from app.patch_service import apply_patch
from app.git_diff import get_diff


async def process_job(ctx, job_id: int):
    db = SessionLocal()

    try:
        job = db.query(Job).filter(Job.id == job_id).first()

        if not job:
            return

        job.status = "running"
        db.commit()

        # Clone repository
        print(f"\nCloning repository for Job {job_id}", flush=True)

        repo_folder = clone_repository(
            job.workspace_id,
            job.repo_url
        )

        # Save workspace for approval & git push
        job.workspace_path = repo_folder
        db.commit()

        print(f"Repository cloned to {repo_folder}", flush=True)

        # Analyze repository
        project = detect_project_type(repo_folder)
        files = find_relevant_files(repo_folder, job.task)
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

            apply_patch(repo_folder, patch)

            diff = get_diff(repo_folder)

            job.diff = diff
            db.commit()

            print("\n===== GIT DIFF =====", flush=True)
            print(diff, flush=True)

        await asyncio.sleep(1)

        job.status = "completed"
        db.commit()

        print(f"\nCompleted Job {job_id}", flush=True)

    except Exception as e:
        print("\n===== ERROR TRACEBACK =====", flush=True)
        traceback.print_exc()
        print(f"\nERROR: {str(e)}", flush=True)

        if "job" in locals() and job:
            job.status = "failed"
            job.ai_result = f"ERROR: {str(e)}"
            db.commit()

    finally:
        db.close()