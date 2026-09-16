from pathlib import Path
from git import Repo

WORKSPACE = Path(__file__).resolve().parent.parent.parent / "workspace"

def clone_repository(workspace_id: str, repo_url: str):
    job_folder = WORKSPACE / workspace_id

    job_folder.mkdir(parents=True, exist_ok=True)

    Repo.clone_from(
        repo_url,
        job_folder,
        depth=1
    )

    return str(job_folder)