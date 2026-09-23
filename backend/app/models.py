from sqlalchemy import BigInteger, Column, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from app.database import Base
import uuid


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    github_id = Column(BigInteger, nullable=False, unique=True, index=True)
    username = Column(String, nullable=False)
    avatar_url = Column(String, nullable=True)
    # Fernet ciphertext; raw OAuth credentials are never stored or serialized.
    access_token = Column(Text, nullable=False)

    jobs = relationship("Job", back_populates="user")


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

    # NULL is reserved for historical jobs whose owner cannot be established.
    # The authenticated jobs API always supplies an owner for new jobs.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    user = relationship("User", back_populates="jobs")

    task = Column(String)
    status = Column(String)

    ai_result = Column(Text, nullable=True)
    diff = Column(Text, nullable=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    # SHA-256 of the opaque random session identifier held in the signed cookie.
    id = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    user = relationship("User")
    name = Column(String, nullable=True)
    csrf_token = Column(String, nullable=False)
    expires_at = Column(Integer, nullable=False, index=True)

    @property
    def github_user_id(self):
        return str(self.user.github_id)

    @property
    def login(self):
        return self.user.username

    @property
    def avatar_url(self):
        return self.user.avatar_url


class OAuthFlow(Base):
    __tablename__ = "oauth_flows"

    state_hash = Column(String, primary_key=True)
    browser_nonce_hash = Column(String, nullable=False)
    verifier_encrypted = Column(Text, nullable=False)
    expires_at = Column(Integer, nullable=False, index=True)
