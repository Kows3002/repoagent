from git import Repo


def commit_and_push(workspace_path: str, token: str):
    repo = Repo(workspace_path)

    # Original GitHub remote
    origin_url = repo.remotes.origin.url

    # Authenticate using the user's PAT
    auth_url = origin_url.replace(
        "https://",
        f"https://x-access-token:{token}@"
    )

    repo.git.remote("set-url", "origin", auth_url)

    # Stage all changes
    repo.git.add(A=True)

    # Commit only if there are changes
    if repo.is_dirty(untracked_files=True):
        repo.index.commit("AI: Apply requested changes")

    # Push to the current branch
    branch = repo.active_branch.name
    repo.git.push("origin", branch)

    return "Changes pushed successfully"