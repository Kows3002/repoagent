from sqlalchemy import Column, Integer, String, Text
from app.database import Base
import uuid


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)

    # Unique workspace for each job
    workspace_id = Column(
        String,
        default=lambda: str(uuid.uuid4()),
        unique=True
    )

    # Path of the cloned repository
    workspace_path = Column(String, nullable=True)

    # Repository details
    repo_url = Column(String)
    github_token = Column(String, nullable=True)

    task = Column(String)
    status = Column(String)

    ai_result = Column(Text, nullable=True)
    diff = Column(Text, nullable=True)