import os
from git import Repo


def commit_and_push(workspace_path: str):
    repo = Repo(workspace_path)

    username = os.getenv("GITHUB_USERNAME")
    token = os.getenv("GITHUB_TOKEN")

    if not username or not token:
        raise Exception("GITHUB_USERNAME or GITHUB_TOKEN is missing")

    # Get the original remote URL dynamically
    origin_url = repo.remotes.origin.url

    # Add authentication to the existing remote
    auth_url = origin_url.replace(
        "https://",
        f"https://{username}:{token}@"
    )

    repo.git.remote("set-url", "origin", auth_url)

    repo.git.add(A=True)

    try:
        repo.index.commit("AI: Apply requested code changes")
    except Exception:
        pass

    repo.git.push("origin", "main")

    return "Changes pushed successfully"