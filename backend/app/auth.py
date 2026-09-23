"""GitHub OAuth, encrypted server-side credentials, and owned-job access."""

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

from app.database import get_db
from app.models import AuthSession, Job, OAuthFlow, User

load_dotenv()

router = APIRouter(prefix="/auth", tags=["Auth"])
logger = logging.getLogger("uvicorn.error.repoagent.auth")
SESSION_COOKIE = "repoagent_session"
FLOW_COOKIE = "repoagent_oauth"
SESSION_SECONDS = 7 * 24 * 60 * 60
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
    return decrypt_token(job.user.access_token)


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


def repository_for_job(repo_url: str, current: AuthSession) -> dict:
    match = REPO_PATTERN.fullmatch(repo_url)
    if not match or match.group(2) in (".", ".."):
        raise HTTPException(status_code=422, detail="Choose a valid GitHub repository.")
    owner, name = match.groups()
    repository = github_request(f"/repos/{owner}/{name}", decrypt_token(current.user.access_token)).json()
    if repository.get("archived") or repository.get("disabled") or not repository.get("permissions", {}).get("push"):
        raise HTTPException(status_code=403, detail="Choose a repository where you have permission to push changes.")
    if not REPO_PATTERN.fullmatch(repository.get("clone_url", "")):
        raise HTTPException(status_code=502, detail="GitHub returned an unavailable repository. Please try again.")
    return repository


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
        return {"authenticated": False, "configured": ready, "user": None}
    return {
        "authenticated": True, "configured": ready,
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
                       "scope": "repo read:user", "state": state,
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
        if not isinstance(token, str) or not token or token_data.get("error"):
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
        db.flush()
        current = AuthSession(
            id=_digest(session_cookie), user_id=account.id,
            name=user.get("name"), csrf_token=secrets.token_urlsafe(32),
            expires_at=int(time.time()) + lifetime,
        )
        previous = _lookup_session(request, db)
        if previous:
            db.delete(previous)
        db.add(current)
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
    db.delete(current)
    db.commit()
    request.session.clear()
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": False}


@router.get("/repositories")
def repositories(response: Response, page: int = Query(1, ge=1, le=10000),
                 current: AuthSession = Depends(get_current_session)):
    response.headers["Cache-Control"] = "no-store"
    result = github_request("/user/repos", decrypt_token(current.user.access_token),
                            {"per_page": 100, "page": page, "sort": "pushed", "direction": "desc",
                             "affiliation": "owner,collaborator,organization_member"})
    keys = ("id", "full_name", "clone_url", "default_branch", "private", "description", "language")
    available = [{key: repository.get(key) for key in keys} for repository in result.json()
                 if repository.get("permissions", {}).get("push")
                 and not repository.get("archived") and not repository.get("disabled")]
    has_more = "next" in result.links
    return {"repositories": available, "has_more": has_more, "next_page": page + 1 if has_more else None}
