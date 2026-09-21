from fastapi import FastAPI
from sqlalchemy import text

from app.database import Base, engine
from app.routes import router
from app import models

# Create tables
Base.metadata.create_all(bind=engine)

# Auto-migrate: add github_token column if missing
with engine.begin() as conn:
    try:
        conn.execute(text("ALTER TABLE jobs ADD COLUMN github_token TEXT"))
        print("github_token column added.")
    except Exception:
        # Column already exists
        pass

app = FastAPI(
    title="RepoAgent API",
    version="1.0.0"
)

app.include_router(router)


@app.get("/")
def home():
    return {
        "name": "RepoAgent",
        "status": "running"
    }


@app.get("/health")
def health():
    return {"ok": True}