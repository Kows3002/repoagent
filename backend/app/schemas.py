from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.git_service import normalize_repo_url
from app.git_push_service import DEFAULT_COMMIT_MESSAGE


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo_url: str = Field(min_length=1, max_length=300)
    task: str = Field(min_length=1, max_length=20000)

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, value: str) -> str:
        return normalize_repo_url(value)


class JobApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit_message: str = Field(default=DEFAULT_COMMIT_MESSAGE, min_length=1, max_length=200, strict=True)

    @field_validator("commit_message", mode="before")
    @classmethod
    def validate_commit_message(cls, value):
        if isinstance(value, str):
            if any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in value):
                raise ValueError("Commit message must be a single line without control characters.")
            return value.strip()
        return value


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
