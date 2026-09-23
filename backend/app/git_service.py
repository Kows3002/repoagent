import base64
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from git import Repo

WORKSPACE = Path(__file__).resolve().parent.parent.parent / "workspace"


class GitCloneError(RuntimeError):
    """A clone failure safe to show without disclosing OAuth credentials."""


def normalize_repo_url(repo_url: str) -> str:
    """Accept only a GitHub HTTPS repository URL before attaching credentials."""
    error = "Use a GitHub repository URL such as https://github.com/owner/repository."
    if not isinstance(repo_url, str) or any(ord(char) < 32 for char in repo_url):
        raise ValueError(error)
    try:
        parsed = urlsplit(repo_url.strip())
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != "github.com"
            or parsed.query
            or parsed.fragment
            or "?" in repo_url
            or "#" in repo_url
        ):
            raise ValueError(error)
        parts = parsed.path.rstrip("/").split("/")
        if len(parts) != 3 or parts[0]:
            raise ValueError(error)
        owner, repository = parts[1:]
        if repository.endswith(".git"):
            repository = repository[:-4]
        if (
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", owner)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository)
            or repository in {".", ".."}
        ):
            raise ValueError(error)
        return f"https://github.com/{owner}/{repository}.git"
    except (ValueError, TypeError):
        raise ValueError(error) from None


def oauth_git_environment(access_token: str) -> dict[str, str]:
    """Provide process-only authentication, never a URL or persisted Git config."""
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError("Sign in with GitHub again before accessing the repository.")
    credentials = base64.b64encode(f"x-access-token:{access_token}".encode()).decode("ascii")
    config = [
        ("http.https://github.com/.extraheader", ""),
        ("http.https://github.com/.extraheader", f"Authorization: Basic {credentials}"),
        ("http.followRedirects", "false"),
        ("credential.helper", ""),
        ("core.askPass", ""),
        ("core.hooksPath", os.devnull),
    ]
    env = {
        "GIT_CONFIG_COUNT": str(len(config)),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_TRACE": "0",
        "GIT_TRACE_CURL": "0",
        "GIT_CURL_VERBOSE": "0",
    }
    for index, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def clone_repository(workspace_id: str, repo_url: str, access_token: str):
    repo_url = normalize_repo_url(repo_url)
    env = oauth_git_environment(access_token)
    # The ID is server-generated; still prevent a caller escaping the workspace.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", workspace_id):
        raise GitCloneError("The repository workspace could not be created.")
    job_folder = WORKSPACE / workspace_id
    job_folder.mkdir(parents=True, exist_ok=True)

    try:
        repo = Repo.clone_from(repo_url, job_folder, depth=1, single_branch=True, env=env)
        repo.close()
    except Exception as error:
        message = str(error).lower()
        if any(value in message for value in ("authentication failed", "bad credentials", "403", "permission denied")):
            raise GitCloneError("GitHub authorization failed. Sign in with GitHub again and check repository access.") from None
        if "repository not found" in message:
            raise GitCloneError("Repository not found. Verify the GitHub repository URL and repository access.") from None
        raise GitCloneError("The repository could not be cloned. Check its GitHub URL and your repository access.") from None
    return str(job_folder)
