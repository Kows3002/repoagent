"""Owned preview jobs and isolated capability-host asset delivery.

Install PreviewHostMiddleware *after* the session middleware so it is outermost.
Preview requests bypass application cookies, cannot reach API routes, and must
present a live session-bound capability through their dedicated hostname.
"""

from __future__ import annotations

import ipaddress
import mimetypes
import os
import re
import time
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import get_current_session, get_owned_job, require_csrf
from app.database import SessionLocal, get_db
from app.models import AuthSession
from app import preview_service as previews


router = APIRouter(tags=["Visual previews"])


def preview_domain() -> str:
    domain = os.environ.get("PREVIEW_DOMAIN", "localhost").strip().lower().rstrip(".")
    if not domain or len(domain) > 180 or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", domain):
        raise previews.PreviewError("Configure a valid dedicated preview domain on the server.", "unsupported")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise previews.PreviewError("Visual previews need a hostname, rather than an IP address.", "unsupported")


def preview_origin(capability: str) -> str:
    domain = preview_domain()
    scheme = os.environ.get("PREVIEW_SCHEME", "http" if domain == "localhost" else "https")
    if scheme not in {"http", "https"} or (scheme != "https" and domain != "localhost"):
        raise previews.PreviewError("Production preview domains must use HTTPS.", "unsupported")
    port = os.environ.get("PREVIEW_PORT", "8000" if domain == "localhost" else "")
    if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
        raise previews.PreviewError("The preview server port is not configured correctly.", "unsupported")
    if not re.fullmatch(r"[a-f0-9]{32}", capability):
        raise previews.PreviewError("This preview link is invalid.")
    return f"{scheme}://{capability}.{domain}{':' + port if port else ''}"


def _metadata(job, current: AuthSession, request: Request) -> dict:
    result = previews.get_preview_status(job.workspace_id, job.id)
    if result["status"] != "ready":
        return result
    try:
        if "PREVIEW_DOMAIN" not in os.environ and request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise previews.PreviewError("Configure PREVIEW_DOMAIN with a dedicated wildcard preview hostname before opening previews on this server.", "unsupported")
        expirations = []
        for variant in ("before", "after"):
            capability, expires_at = previews.create_capability(job.workspace_id, job.id, variant, current.id, current.expires_at)
            result[f"{variant}_url"] = preview_origin(capability) + "/"
            expirations.append(expires_at)
        result["expires_at"] = min(expirations)
        return result
    except previews.PreviewError as error:
        return {"status": error.status, "message": str(error)}


@router.get("/jobs/{job_id}/preview")
def get_job_preview(job_id: int, request: Request, response: Response, current: AuthSession = Depends(get_current_session), db: Session = Depends(get_db)):
    job = get_owned_job(job_id, current, db)
    response.headers["Cache-Control"] = "no-store"
    return _metadata(job, current, request)


@router.post("/jobs/{job_id}/preview", status_code=202)
def create_job_preview(job_id: int, request: Request, response: Response, background_tasks: BackgroundTasks, current: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    job = get_owned_job(job_id, current, db)
    if job.status != "completed":
        raise HTTPException(status_code=409, detail="Wait for the code change to finish before building its visual preview.")
    response.headers["Cache-Control"] = "no-store"
    existing = previews.get_preview_status(job.workspace_id, job.id)
    if existing["status"] in {"ready", "building"}:
        return _metadata(job, current, request)
    background_tasks.add_task(previews.build_previews, job.workspace_id, job.id)
    return {"status": "building", "message": "Building isolated previews of the original and updated repository."}


def _capability_owner(capability: str, db: Session):
    claims = previews.read_capability(capability)
    if not claims:
        raise HTTPException(status_code=404, detail="This preview link has expired. Refresh the preview in RepoAgent.")
    current = db.get(AuthSession, claims.get("session_id"))
    if current is None or current.expires_at <= int(time.time()):
        raise HTTPException(status_code=404, detail="This preview link has expired. Sign in and refresh the preview in RepoAgent.")
    job = get_owned_job(claims["job_id"], current, db)
    if job.workspace_id != claims.get("workspace_id"):
        raise HTTPException(status_code=404, detail="This preview is no longer available.")
    return claims, job


def _asset_headers(capability: str) -> dict[str, str]:
    origin = preview_origin(capability)
    frontend = urlsplit(os.environ.get("FRONTEND_URL", "http://127.0.0.1:5173"))
    ancestors = f"{frontend.scheme}://{frontend.netloc}" if frontend.scheme in {"http", "https"} and frontend.netloc else "'none'"
    if preview_domain() == "localhost":
        ancestors = "http://127.0.0.1:5173 http://localhost:5173 " + ancestors
    policy = "; ".join([
        "default-src 'none'", f"script-src {origin} 'unsafe-inline' 'wasm-unsafe-eval'", f"style-src {origin} 'unsafe-inline'",
        f"img-src {origin} data: blob:", f"font-src {origin} data:", f"media-src {origin} blob:",
        "connect-src 'none'", "frame-src 'none'", "worker-src 'none'", "object-src 'none'", "base-uri 'none'",
        "form-action 'none'", f"frame-ancestors {ancestors}", "sandbox allow-scripts",
    ])
    return {
        "Content-Security-Policy": policy,
        "Cache-Control": "private, no-store, max-age=0",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    }


def _asset_response(capability: str, asset_path: str, method: str):
    try:
        with SessionLocal() as db:
            claims, _ = _capability_owner(capability, db)
        headers = _asset_headers(capability)
        if method == "OPTIONS":
            return Response(status_code=204, headers=headers)
        if method not in {"GET", "HEAD"}:
            return PlainTextResponse("Preview assets are read-only.", status_code=405, headers={**headers, "Allow": "GET, HEAD, OPTIONS"})
        state = previews.get_preview_status(claims["workspace_id"])
        if state["status"] != "ready":
            raise HTTPException(status_code=404, detail="The visual preview is not ready yet.")
        target = previews.resolve_asset(claims["workspace_id"], claims["variant"], asset_path, spa=state.get("kind") == "vite")
        if target is None:
            raise HTTPException(status_code=404, detail="This preview asset was not found.")
        types = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html", ".wasm": "application/wasm", ".svg": "image/svg+xml"}
        media_type = types.get(target.suffix.lower()) or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return FileResponse(target, media_type=media_type, headers=headers)
    except HTTPException as error:
        return PlainTextResponse(error.detail, status_code=error.status_code, headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; frame-ancestors *; sandbox", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
    except (OSError, previews.PreviewError):
        return PlainTextResponse("This preview could not be opened. Refresh the preview in RepoAgent.", status_code=404, headers={"Cache-Control": "no-store"})


@router.get("/previews/{job_id}/{variant}/{capability}/{asset_path:path}", include_in_schema=False)
def preview_asset_redirect(job_id: int, variant: str, capability: str, asset_path: str, db: Session = Depends(get_db)):
    """Legacy URL shape redirects; repository HTML is never served on the API origin."""
    claims, _ = _capability_owner(capability, db)
    if claims["job_id"] != job_id or claims["variant"] != variant or previews.resolve_asset(claims["workspace_id"], variant, asset_path or "index.html", spa=True) is None:
        raise HTTPException(status_code=404, detail="Preview asset not found.")
    return RedirectResponse(preview_origin(capability) + "/" + quote(asset_path, safe="/"), status_code=307, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


class PreviewHostMiddleware:
    """A preview host has only a read-only artifact surface, never API endpoints."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        host_header = dict(scope.get("headers", [])).get(b"host", b"").decode("latin-1")
        try:
            host = (urlsplit("//" + host_header).hostname or "").lower().rstrip(".")
            domain = preview_domain()
        except (ValueError, previews.PreviewError):
            await self.app(scope, receive, send)
            return
        suffix = "." + domain
        if not host.endswith(suffix):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        capability = host[:-len(suffix)]
        if not re.fullmatch(r"[a-f0-9]{32}", capability):
            response = PlainTextResponse("Preview not found.", status_code=404)
        else:
            response = await run_in_threadpool(_asset_response, capability, scope.get("path", "/"), scope.get("method", "GET"))
        await response(scope, receive, send)
