import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.auth import configured_origins, logger as auth_logger, router as auth_router
from app.database import engine
from app.migrations import initialize_database
from app.routes import router
from app.preview_routes import PreviewHostMiddleware, router as preview_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database(engine)
    yield


def create_app() -> FastAPI:
    secret = os.getenv("SESSION_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("Set SESSION_SECRET to a persistent random value of at least 32 characters.")
    secure = os.getenv("SESSION_HTTPS_ONLY", "").lower()
    https_only = secure == "true" if secure else os.getenv("GITHUB_CALLBACK_URL", "").strip().startswith("https://")
    same_site = os.getenv("SESSION_SAME_SITE", "none" if https_only else "lax").strip().lower()
    if same_site not in {"lax", "none"}:
        raise RuntimeError("SESSION_SAME_SITE must be lax or none.")
    if same_site == "none" and not https_only:
        raise RuntimeError(
            "SESSION_SAME_SITE=none requires secure cookies. "
            "Set SESSION_HTTPS_ONLY=true and serve the API over HTTPS."
        )
    auth_logger.info("Session configuration: cookie=repoagent_session secure=%s same_site=%s allowed_origins=%s",
                     https_only, same_site, sorted(configured_origins()))
    application = FastAPI(title="RepoAgent API", version="1.0.0", lifespan=lifespan)
    application.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="repoagent_session",
        max_age=60 * 60 * 24 * 7,
        same_site=same_site,
        https_only=https_only,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(configured_origins()),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )

    @application.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/auth/", "/jobs")):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    application.include_router(auth_router)
    application.include_router(router)
    application.include_router(preview_router)

    @application.get("/")
    def home():
        return {"name": "RepoAgent", "status": "running"}

    @application.get("/health")
    def health():
        return {"ok": True}

    # Preview hosts are intercepted before sessions, CORS, or application routes.
    application.add_middleware(PreviewHostMiddleware)
    return application


app = create_app()
