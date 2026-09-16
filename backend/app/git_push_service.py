from git import Repo

def commit_and_push(repo_folder: str):
    repo = Repo(repo_folder)

    repo.git.add(A=True)

    if not repo.is_dirty(untracked_files=True):
        return "No changes to push"

    repo.index.commit("AI: Apply approved code changes")

    origin = repo.remote("origin")
    origin.push()

    return "Changes pushed successfully"