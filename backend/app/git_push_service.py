from git import Actor, Repo

from app.git_service import normalize_repo_url, oauth_git_environment


class GitPushError(RuntimeError):
    """A user-safe push failure that never contains OAuth credentials."""


def _default_branch(repo: Repo, origin_url: str) -> str:
    refs = repo.git.ls_remote("--symref", "--exit-code", origin_url, "HEAD", "refs/heads/*")
    default = None
    heads = set()
    for line in refs.splitlines():
        value, separator, name = line.partition("\t")
        if not separator:
            continue
        if name == "HEAD" and value.startswith("ref: refs/heads/"):
            default = value.removeprefix("ref: refs/heads/")
        elif name.startswith("refs/heads/"):
            heads.add(name.removeprefix("refs/heads/"))
    if not default or default not in heads:
        raise GitPushError("The repository has no existing default branch. Add an initial commit before generating changes.")
    return default


def commit_and_push(workspace_path: str, access_token: str):
    repo = None
    try:
        repo = Repo(workspace_path)
        origin_url = normalize_repo_url(repo.remotes.origin.url)
        env = oauth_git_environment(access_token)
        with repo.git.custom_environment(**env):
            default_branch = _default_branch(repo, origin_url)
        if repo.head.is_detached or repo.active_branch.name != default_branch:
            raise GitPushError("The repository's default branch changed. Generate and review the changes again before approving.")

        repo.git.add(A=True)
        if repo.is_dirty(untracked_files=True):
            identity = Actor("RepoAgent", "repoagent@users.noreply.github.com")
            repo.index.commit(
                "RepoAgent: Apply requested changes",
                author=identity,
                committer=identity,
                skip_hooks=True,
            )

        # An explicit refspec avoids configured push destinations. A normal push
        # refuses concurrent upstream changes; never force, branch, or open a PR.
        with repo.git.custom_environment(**env):
            repo.git.push(origin_url, f"HEAD:refs/heads/{default_branch}")
    except GitPushError:
        raise
    except Exception as error:
        message = str(error).lower()
        if any(value in message for value in ("authentication failed", "bad credentials", "403", "permission denied", "write access")):
            raise GitPushError("GitHub authorization failed. Sign in with GitHub again and check repository write access.") from None
        if "repository not found" in message:
            raise GitPushError("Repository not found. Verify the GitHub repository URL and repository access.") from None
        if any(value in message for value in ("non-fast-forward", "fetch first", "stale info")):
            raise GitPushError("The default branch changed after generation. Generate and review the changes again before approving.") from None
        if any(value in message for value in ("protected branch", "gh006", "gh013", "repository rule")):
            raise GitPushError("GitHub's branch protection rules rejected the direct push. Check the repository rules and your write access.") from None
        raise GitPushError("Changes could not be pushed. Check repository write access and try again.") from None
    finally:
        if repo is not None:
            repo.close()

    return "Changes pushed successfully"
