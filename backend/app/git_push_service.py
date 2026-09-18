from git import Repo

def commit_and_push(workspace_path: str, token: str):
    repo = Repo(workspace_path)

    origin_url = repo.remotes.origin.url

    auth_url = origin_url.replace(
        "https://",
        f"https://x-access-token:{token}@"
    )

    repo.git.remote("set-url", "origin", auth_url)

    repo.git.add(A=True)

    try:
        repo.index.commit("AI: Apply requested changes")
    except Exception:
        pass

    repo.git.push("origin", "main")

    return "Changes pushed successfully"