from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.git_service import normalize_repo_url


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo_url: str = Field(min_length=1, max_length=300)
    task: str = Field(min_length=1, max_length=20000)

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, value: str) -> str:
        return normalize_repo_url(value)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    github_id: int
    username: str
    avatar_url: str | None = None


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    repo_url: str
    task: str
    status: str
    ai_result: str | None = None
    diff: str | None = None
