from pydantic import BaseModel

class JobCreate(BaseModel):
    repo_url: str
    task: str


class JobResponse(BaseModel):
    id: int
    repo_url: str
    task: str
    status: str
    ai_result: str | None = None
    diff: str | None = None

    class Config:
        from_attributes = True