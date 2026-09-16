from git import Repo

def get_diff(repo_folder: str):
    repo = Repo(repo_folder)
    return repo.git.diff()