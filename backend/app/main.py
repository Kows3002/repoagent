from fastapi import FastAPI

from app.database import Base, engine
from app.routes import router
from app import models

Base.metadata.create_all(bind=engine)

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