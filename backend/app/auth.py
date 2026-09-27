"""GitHub App authorization, selected repository access, and owned-job access."""

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
import httpx
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.activity import record_activity
from app.database import get_db
from app.models import ActivityEvent, AuthSession, Job, OAuthFlow, User
from app.schemas import ActivityResponse

load_dotenv()

router = APIRouter(prefix="/auth", tags=["Auth"])
logger = logging.getLogger("uvicorn.error.repoagent.auth")
SESSION_COOKIE = "repoagent_session"
FLOW_COOKIE = "repoagent_oauth"
SESSION_SECONDS = 8 * 60 * 60
FLOW_SECONDS = 10 * 60
GITHUB_API = "https://api.github.com"
REPO_PATTERN = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?\Z")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _valid_app_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and (parsed.scheme == "https" or (
                parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
            ))
        )
    except ValueError:
        return False


def configured() -> bool:
    return bool(
        os.getenv("GITHUB_CLIENT_ID") and os.getenv("GITHUB_CLIENT_SECRET")
        and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", os.getenv("GITHUB_APP_SLUG", ""))
        and len(application_secret()) >= 32
        and _valid_app_url(os.getenv("GITHUB_CALLBACK_URL", "").strip())
        and _valid_app_url(os.getenv("FRONTEND_URL", "").strip())
    )


def _oauth_url(name: str) -> str:
    """Use the deployment's explicit URL; never fall back to a local address."""
    value = os.getenv(name, "").strip()
    if not _valid_app_url(value):
        raise HTTPException(status_code=503, detail=f"Configure {name} with a valid application URL.")
    return value


def application_secret() -> str:
    """One persistent deployment secret can protect sessions and encryption."""
    return os.getenv("APP_SECRET") or os.getenv("SESSION_SECRET", "")


def configured_origins() -> set[str]:
    candidates = [os.getenv("FRONTEND_URL", "")]
    candidates.extend(os.getenv("CORS_ORIGINS", "").split(","))
    origins = set()
    for candidate in candidates:
        candidate = candidate.strip()
        if _valid_app_url(candidate):
            parts = urlsplit(candidate)
            origins.add(f"{parts.scheme}://{parts.netloc}")
    return origins


def _cipher() -> Fernet:
    secret = application_secret()
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail="GitHub sign-in is not configured yet.")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_token(token: str) -> str:
    return _cipher().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    try:
        return _cipher().decrypt(encrypted.encode()).decode()
    except (InvalidToken, AttributeError, UnicodeError):
        raise HTTPException(status_code=401, detail="Your GitHub connection expired. Please sign in again.") from None


def token_for_job(job: Job) -> str:
    if not job.user_id or job.user is None:
        raise HTTPException(status_code=401, detail="Please create a new job after signing in with GitHub.")
    token = _user_token(job.user)
    _repository_with_token(job.repo_url, token)
    return token


def _secure_cookie() -> bool:
    explicit = os.getenv("SESSION_HTTPS_ONLY", "").lower()
    return explicit == "true" if explicit else os.getenv("GITHUB_CALLBACK_URL", "").strip().startswith("https://")


def _frontend_redirect(error: str | None = None) -> RedirectResponse:
    frontend = _oauth_url("FRONTEND_URL")
    logger.info("Redirect URL: %s outcome=%s", frontend, error or "success")
    parts = urlsplit(frontend)
    query = urlencode({"auth_error": error}) if error else ""
    response = RedirectResponse(urlunsplit((parts.scheme, parts.netloc, parts.path or "/", query, "")), status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(FLOW_COOKIE, path="/auth", secure=_secure_cookie(), httponly=True, samesite="lax")
    return response


def _rollback_oauth_failure(db: Session) -> None:
    """Keep a failed database connection from masking the safe login redirect."""
    try:
        db.rollback()
    except SQLAlchemyError:
        pass


def _lookup_session(request: Request, db: Session) -> AuthSession | None:
    cookie = request.session.get("session_id", "")
    if not isinstance(cookie, str) or not cookie or len(cookie) > 256:
        request.state.auth_session_reason = (
            "cookie_invalid_or_expired" if request.cookies.get(SESSION_COOKIE) else "cookie_missing"
        )
        return None
    current = db.get(AuthSession, _digest(cookie))
    if current and current.expires_at > int(time.time()) and current.user is not None:
        if (current.github_app_client_id != os.getenv("GITHUB_CLIENT_ID")
                or not _current_app_credential(current.user)):
            request.state.auth_session_reason = "github_app_reauthorization_required"
            return None
        request.state.auth_session_reason = "authenticated"
        return current
    request.state.auth_session_reason = (
        "server_session_missing" if current is None else
        "server_session_expired" if current.expires_at <= int(time.time()) else "user_missing"
    )
    return None


def get_current_session(request: Request, db: Session = Depends(get_db)) -> AuthSession:
    current = _lookup_session(request, db)
    if current is None:
        raise HTTPException(status_code=401, detail="Sign in with GitHub to continue.")
    return current


def require_csrf(request: Request, current: AuthSession = Depends(get_current_session)) -> AuthSession:
    origin = request.headers.get("Origin", "")
    if not origin:
        referer = request.headers.get("Referer", "")
        try:
            parts = urlsplit(referer)
            origin = f"{parts.scheme}://{parts.netloc}" if parts.hostname else ""
        except ValueError:
            origin = ""
    if origin not in configured_origins():
        raise HTTPException(status_code=403, detail="This request did not come from RepoAgent. Reload the page and try again.")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied or not hmac.compare_digest(supplied.encode(), current.csrf_token.encode()):
        raise HTTPException(status_code=403, detail="Your session needs to be refreshed. Reload the page and try again.")
    return current


def get_current_user(current: AuthSession = Depends(get_current_session)) -> User:
    """Compatibility adapter for callers using the existing User model."""
    return current.user


def get_owned_job(job_id: int, current: AuthSession, db: Session) -> Job:
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == current.user_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


def github_request(path: str, token: str, params: dict | None = None) -> httpx.Response:
    try:
        response = httpx.get(
            GITHUB_API + path, params=params, timeout=20,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "RepoAgent"},
        )
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="GitHub could not be reached. Please try again.") from None
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="Your GitHub connection expired. Please sign in again.")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Repository not found. Check repository access and try again.")
    if response.status_code in (403, 429):
        raise HTTPException(status_code=403, detail="GitHub could not grant access. Check repository permissions or try again shortly.")
    if not response.is_success:
        raise HTTPException(status_code=502, detail="GitHub could not complete the request. Please try again.")
    return response


def repository_access() -> dict:
    ready = configured()
    return {
        "configured": ready,
        "installation_url": f"https://github.com/apps/{os.environ['GITHUB_APP_SLUG']}/installations/new" if ready else None,
        "manage_url": "https://github.com/settings/installations",
    }


def _current_app_credential(user: User) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", os.getenv("GITHUB_APP_SLUG", ""))
                and os.getenv("GITHUB_CLIENT_ID") and len(application_secret()) >= 32
                and user.github_app_client_id == os.getenv("GITHUB_CLIENT_ID")
                and user.github_token_expires_at and user.github_token_expires_at > int(time.time()))


def _user_token(user: User) -> str:
    if not _current_app_credential(user):
        raise HTTPException(status_code=401, detail="Sign in with GitHub again to choose repository access.")
    token = decrypt_token(user.access_token)
    if not token.startswith("ghu_"):
        raise HTTPException(status_code=401, detail="Sign in with GitHub again to choose repository access.")
    return token


def _github_collection(response: httpx.Response, key: str) -> list[dict]:
    try:
        data = response.json()
        items = data[key]
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError()
        return items
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=502, detail="GitHub returned an unavailable repository list. Please try again.") from None


def _installations(token: str) -> list[dict]:
    installations = []
    for page in range(1, 10001):
        result = github_request("/user/installations", token, {"per_page": 100, "page": page})
        for item in _github_collection(result, "installations"):
            # The endpoint is scoped to the authorizing App. Check the configured
            # slug too, so mismatched operator credentials fail closed.
            if (item.get("app_slug") == os.environ["GITHUB_APP_SLUG"]
                    and isinstance(item.get("id"), int) and item["id"] > 0
                    and not item.get("suspended_at")):
                installations.append(item)
        if "next" not in result.links:
            return sorted(installations, key=lambda item: item["id"])
    raise HTTPException(status_code=502, detail="GitHub returned too many installation pages. Please try again.")


def _installation_summary(item: dict) -> dict:
    account = item.get("account", {})
    login = account.get("login", "")
    management = f"https://github.com/settings/installations/{item['id']}"
    if account.get("type") == "Organization" and re.fullmatch(r"[A-Za-z0-9-]+", login):
        management = f"https://github.com/organizations/{login}/settings/installations/{item['id']}"
    return {"id": item["id"], "account": login,
            "repository_selection": item.get("repository_selection", "selected"), "manage_url": management}


def _repository_page(installation: dict, token: str, page: int):
    result = github_request(f"/user/installations/{installation['id']}/repositories", token,
                            {"per_page": 100, "page": page})
    repos = _github_collection(result, "repositories")
    # User push permission alone is insufficient: the App must also have write.
    writable = installation.get("permissions", {}).get("contents") == "write"
    return ([repo for repo in repos if writable and repo.get("permissions", {}).get("push")
             and not repo.get("archived") and not repo.get("disabled")
             and REPO_PATTERN.fullmatch(repo.get("clone_url", ""))], "next" in result.links)


def _repository_with_token(repo_url: str, token: str) -> dict:
    match = REPO_PATTERN.fullmatch(repo_url)
    if not match or match.group(2) in (".", ".."):
        raise HTTPException(status_code=422, detail="Choose a valid GitHub repository.")
    owner, name = match.groups()
    full_name = f"{owner}/{name}".lower()
    # App tokens can read public metadata outside an installation. Listing the
    # installation repositories, rather than GET /repos, proves explicit access.
    for installation in _installations(token):
        if installation.get("account", {}).get("login", "").lower() != owner.lower():
            continue
        if installation.get("permissions", {}).get("contents") != "write":
            continue
        for page in range(1, 10001):
            repositories, has_more = _repository_page(installation, token, page)
            for repository in repositories:
                if repository.get("full_name", "").lower() == full_name:
                    return repository
            if not has_more:
                break
        else:
            raise HTTPException(status_code=502, detail="GitHub returned too many repository pages. Please try again.")
    raise HTTPException(status_code=403, detail="Choose this repository in your GitHub App installation and allow Contents read and write access.")


def repository_for_job(repo_url: str, current: AuthSession) -> dict:
    # Validate before reaching GitHub even when a client bypasses the picker.
    if not REPO_PATTERN.fullmatch(repo_url):
        raise HTTPException(status_code=422, detail="Choose a valid GitHub repository.")
    return _repository_with_token(repo_url, _user_token(current.user))


class SessionUser(BaseModel):
    id: int
    username: str
    avatar_url: str | None
    # Compatibility fields consumed by the existing frontend.
    login: str
    name: str | None
    user_id: int
    github_id: int


class SessionResponse(BaseModel):
    authenticated: bool
    user: SessionUser | None
    configured: bool
    csrf_token: str | None = None
    repository_access: dict


@router.get("/session", response_model=SessionResponse, response_model_exclude_unset=True)
def session_status(request: Request, response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    try:
        current = _lookup_session(request, db)
    except SQLAlchemyError as failure:
        logger.warning("Session lookup failed: reason=database_unavailable error_type=%s", type(failure).__name__)
        raise HTTPException(status_code=503, detail="Session storage is unavailable. Please try again.") from None
    logger.info("Session checked: authenticated=%s reason=%s", bool(current), request.state.auth_session_reason)
    ready = configured()
    if not current:
        return {"authenticated": False, "configured": ready, "user": None, "repository_access": repository_access()}
    return {
        "authenticated": True, "configured": ready, "repository_access": repository_access(),
        "user": {"id": int(current.github_user_id), "username": current.user.username,
                 "avatar_url": current.avatar_url, "login": current.login,
                 "name": current.name, "user_id": current.user_id,
                 "github_id": current.user.github_id},
        "csrf_token": current.csrf_token,
    }


@router.get("/me")
def current_user(response: Response, current: AuthSession = Depends(get_current_session)):
    response.headers["Cache-Control"] = "no-store"
    return {"id": current.user.id, "github_id": current.user.github_id,
            "username": current.user.username, "avatar_url": current.user.avatar_url,
            "csrf_token": current.csrf_token}


@router.get("/github/login")
def github_login(db: Session = Depends(get_db)):
    if not configured():
        logger.warning("OAuth start failed: reason=not_configured")
        return _frontend_redirect("not_configured")
    logger.info("OAuth started: callback_url=%s", _oauth_url("GITHUB_CALLBACK_URL"))
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    now = int(time.time())
    db.query(OAuthFlow).filter(OAuthFlow.expires_at <= now).delete(synchronize_session=False)
    db.query(AuthSession).filter(AuthSession.expires_at <= now).delete(synchronize_session=False)
    db.add(OAuthFlow(state_hash=_digest(state), browser_nonce_hash=_digest(nonce),
                     verifier_encrypted=encrypt_token(verifier), expires_at=now + FLOW_SECONDS))
    db.commit()
    query = urlencode({"client_id": os.environ["GITHUB_CLIENT_ID"],
                       "redirect_uri": _oauth_url("GITHUB_CALLBACK_URL"),
                       "state": state,
                       "code_challenge": challenge, "code_challenge_method": "S256"})
    response = RedirectResponse("https://github.com/login/oauth/authorize?" + query, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(FLOW_COOKIE, nonce, max_age=FLOW_SECONDS, httponly=True,
                        secure=_secure_cookie(), samesite="lax", path="/auth")
    return response


@router.get("/github/callback")
def github_callback(request: Request, code: str = "", state: str = "", error: str = "", db: Session = Depends(get_db)):
    logger.info("Callback received: code_present=%s state_present=%s flow_cookie_present=%s provider_error=%s",
                bool(code), bool(state), bool(request.cookies.get(FLOW_COOKIE)), bool(error))
    if not configured():
        return _frontend_redirect("not_configured")
    nonce = request.cookies.get(FLOW_COOKIE, "")
    # A signed-in session cannot replace the browser-bound OAuth flow cookie.
    # Reject incomplete callbacks before touching the database or GitHub.
    if not state or len(state) > 256 or not nonce or len(nonce) > 256:
        return _frontend_redirect("invalid_state")
    try:
        flow = db.get(OAuthFlow, _digest(state))
        if (flow is None or flow.expires_at <= int(time.time())
                or not hmac.compare_digest(flow.browser_nonce_hash, _digest(nonce))):
            return _frontend_redirect("invalid_state")
        verifier = decrypt_token(flow.verifier_encrypted)
        # Consume the one-time state before exchanging the authorization code.
        db.delete(flow)
        db.commit()
    except (HTTPException, SQLAlchemyError) as failure:
        logger.warning("OAuth callback failed: stage=state_validation error_type=%s", type(failure).__name__)
        _rollback_oauth_failure(db)
        return _frontend_redirect("sign_in_failed")
    if error or not code or len(code) > 2048:
        return _frontend_redirect("access_denied" if error == "access_denied" else "sign_in_failed")
    stage = "token_exchange"
    try:
        exchanged = httpx.post(
            "https://github.com/login/oauth/access_token", timeout=20,
            headers={"Accept": "application/json", "User-Agent": "RepoAgent"},
            data={"client_id": os.environ["GITHUB_CLIENT_ID"],
                  "client_secret": os.environ["GITHUB_CLIENT_SECRET"], "code": code,
                  "redirect_uri": _oauth_url("GITHUB_CALLBACK_URL"), "code_verifier": verifier},
        )
        exchanged.raise_for_status()
        token_data = exchanged.json()
        token = token_data.get("access_token")
        if (not isinstance(token, str) or not token.startswith("ghu_")
                or token_data.get("scope") or token_data.get("error")):
            logger.warning("OAuth callback failed: stage=token_exchange reason=invalid_token_response")
            return _frontend_redirect("sign_in_failed")
        logger.info("Token exchanged")
        stage = "user_fetch"
        user = github_request("/user", token).json()
        if not isinstance(user.get("id"), int) or not user.get("login"):
            logger.warning("OAuth callback failed: stage=user_fetch reason=invalid_profile")
            return _frontend_redirect("sign_in_failed")
        logger.info("User fetched")
        stage = "session_storage"
        lifetime = min(SESSION_SECONDS, max(1, int(token_data.get("expires_in", SESSION_SECONDS))))
        session_cookie = secrets.token_urlsafe(48)
        account = db.query(User).filter(User.github_id == user["id"]).first()
        if account is None:
            account = User(github_id=user["id"])
            db.add(account)
        account.username = user["login"]
        account.avatar_url = user.get("avatar_url")
        account.access_token = encrypt_token(token)
        account.github_app_client_id = os.environ["GITHUB_CLIENT_ID"]
        expires_at = int(time.time()) + lifetime
        account.github_token_expires_at = expires_at
        db.flush()
        current = AuthSession(
            id=_digest(session_cookie), user_id=account.id,
            name=user.get("name"), csrf_token=secrets.token_urlsafe(32),
            github_app_client_id=os.environ["GITHUB_CLIENT_ID"],
            expires_at=expires_at,
        )
        previous = _lookup_session(request, db)
        if previous:
            db.delete(previous)
        db.add(current)
        record_activity(db, account.id, "login")
        db.commit()
        account_id = account.id
    except (httpx.HTTPError, HTTPException, SQLAlchemyError, ValueError, TypeError, KeyError, AttributeError) as failure:
        logger.warning("OAuth callback failed: stage=%s error_type=%s", stage, type(failure).__name__)
        _rollback_oauth_failure(db)
        return _frontend_redirect("sign_in_failed")
    # Only identifiers enter the signed cookie, never OAuth credentials.
    # The DB session remains authoritative for user lookup, expiry and revocation.
    request.session.clear()
    request.session["session_id"] = session_cookie
    request.session["user_id"] = account_id
    logger.info("Session created")
    return _frontend_redirect()


@router.post("/logout")
def logout(request: Request, response: Response, current: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    record_activity(db, current.user_id, "logout")
    db.delete(current)
    db.commit()
    request.session.clear()
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": False}


@router.get("/repositories")
def repositories(response: Response, cursor: str | None = Query(None, max_length=32),
                 current: AuthSession = Depends(get_current_session)):
    response.headers["Cache-Control"] = "no-store"
    token = _user_token(current.user)
    installations = _installations(token)
    access = {**repository_access(), "installations": [_installation_summary(item) for item in installations]}
    if not installations:
        return {"repositories": [], "has_more": False, "next_cursor": None, "access": access}
    index, page = 0, 1
    if cursor is not None:
        match = re.fullmatch(r"([1-9][0-9]{0,19}):([1-9][0-9]{0,4})", cursor)
        if not match or int(match.group(2)) > 10000:
            raise HTTPException(status_code=422, detail="Refresh the repository list to continue.")
        installation_id, page = map(int, match.groups())
        index = next((i for i, item in enumerate(installations) if item["id"] == installation_id), -1)
        if index < 0:
            raise HTTPException(status_code=403, detail="Repository access changed. Refresh the repository list.")
    available, has_more = _repository_page(installations[index], token, page)
    if has_more and page == 10000:
        raise HTTPException(status_code=502, detail="GitHub returned too many repository pages. Please try again.")
    next_cursor = (f"{installations[index]['id']}:{page + 1}" if has_more else
                   f"{installations[index + 1]['id']}:1" if index + 1 < len(installations) else None)
    keys = ("id", "full_name", "clone_url", "default_branch", "private", "description", "language")
    return {"repositories": [{key: repo.get(key) for key in keys} for repo in available],
            "has_more": next_cursor is not None, "next_cursor": next_cursor, "access": access}


@router.get("/activity", response_model=list[ActivityResponse])
def account_activity(
    response: Response, limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db), current: AuthSession = Depends(get_current_session),
):
    response.headers["Cache-Control"] = "no-store"
    return (db.query(ActivityEvent)
            .filter(ActivityEvent.user_id == current.user_id)
            .order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc())
            .limit(limit).all())
