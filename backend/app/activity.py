"""Account activity uses fixed summaries, never request bodies or credentials."""

from app.models import ActivityEvent


SUMMARIES = {
    "login": "Signed in with GitHub.",
    "logout": "Signed out of RepoAgent.",
    "job_created": "Created a code change job.",
    "job_completed": "Code change is ready for review.",
    "job_failed": "Code change could not be generated.",
    "job_pushed": "Approved and pushed changes to GitHub.",
}


def record_activity(db, user_id: int | None, kind: str, job_id: int | None = None):
    """Participate in the caller's transaction so activity matches saved state."""
    message = SUMMARIES[kind]
    if user_id is not None:
        db.add(ActivityEvent(user_id=user_id, kind=kind, message=message, job_id=job_id))
