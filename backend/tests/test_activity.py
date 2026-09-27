"""Persisted history and approval tests use only isolated SQLite and mocked Git."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth, routes, worker
from app.activity import record_activity
from app.database import Base, get_db
from app.git_push_service import GitPushError, PushResult
from app.models import ActivityEvent, Job, User


class ActivityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.stack.callback(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.factory() as db:
            db.add_all([User(id=1, github_id=1, username="first", access_token="encrypted"),
                        User(id=2, github_id=2, username="second", access_token="encrypted")])
            db.commit()
        self.current = SimpleNamespace(user_id=1)
        app = FastAPI()
        app.include_router(auth.router)
        app.include_router(routes.router)
        def isolated_db():
            with self.factory() as db:
                yield db
        app.dependency_overrides[get_db] = isolated_db
        app.dependency_overrides[auth.get_current_session] = lambda: self.current
        app.dependency_overrides[auth.require_csrf] = lambda: self.current
        self.client = self.stack.enter_context(TestClient(app))
        self.stack.enter_context(patch.object(routes, "repository_for_job", return_value={"clone_url": "https://github.com/first/project.git"}))
        self.stack.enter_context(patch.object(routes, "token_for_job", return_value="unused-oauth-token"))

    def seed_job(self, **values):
        defaults = dict(user_id=1, repo_url="https://github.com/first/project.git", task="Change src/App.jsx", status="completed", diff="-old\n+new", workspace_path="test-repository")
        defaults.update(values)
        with self.factory() as db:
            job = Job(**defaults)
            db.add(job)
            db.commit()
            return job.id

    def test_job_creation_and_history_survive_new_sessions(self):
        with patch.object(routes, "run_job") as run:
            response = self.client.post("/jobs", json={"repo_url": "https://github.com/first/project.git", "task": "Change title"})
        self.assertEqual(response.status_code, 202)
        job = response.json()
        self.assertIsInstance(job["created_at"], int)
        self.assertIsInstance(job["updated_at"], int)
        self.assertIsNone(job["pushed_at"])
        run.assert_called_once_with(job["id"])
        history = self.client.get("/jobs").json()
        self.assertEqual(history[0]["id"], job["id"])
        events = self.client.get("/auth/activity").json()
        self.assertEqual(events[0]["kind"], "job_created")
        self.assertEqual(events[0]["job_id"], job["id"])
        self.assertEqual(set(events[0]), {"id", "kind", "message", "job_id", "created_at"})
        self.assertNotIn("unused-oauth-token", response.text)

    def test_history_is_recent_capped_and_owner_scoped(self):
        ids = [self.seed_job() for _ in range(53)]
        other = self.seed_job(user_id=2)
        orphan = self.seed_job(user_id=None)
        history = self.client.get("/jobs").json()
        self.assertEqual([job["id"] for job in history], list(reversed(ids[-50:])))
        self.assertEqual(self.client.get(f"/jobs/{other}").status_code, 404)
        self.assertEqual(self.client.get(f"/jobs/{orphan}").status_code, 404)

    def test_approved_push_persists_actual_message_and_retries_do_not_push(self):
        job_id = self.seed_job(updated_at=1)
        with patch.object(routes, "commit_and_push", return_value=PushResult("Actual committed title")) as push:
            first = self.client.post(f"/jobs/{job_id}/approve", json={"commit_message": "Requested title"})
            second = self.client.post(f"/jobs/{job_id}/approve", json={"commit_message": "Different retry"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(first.json()["commit_message"], "Actual committed title")
        push.assert_called_once_with("test-repository", "unused-oauth-token", "Requested title")
        job = self.client.get(f"/jobs/{job_id}").json()
        self.assertEqual(job["pushed_at"], first.json()["pushed_at"])
        self.assertEqual(job["commit_message"], "Actual committed title")
        self.assertGreater(job["updated_at"], 1)
        self.assertEqual([event["kind"] for event in self.client.get("/auth/activity").json()], ["job_pushed"])

    def test_concurrent_duplicate_approvals_push_only_once(self):
        job_id = self.seed_job()
        with patch.object(routes, "commit_and_push", return_value=PushResult("One commit")) as push:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.client.post(f"/jobs/{job_id}/approve"), range(2)))
        self.assertEqual([response.status_code for response in results], [200, 200])
        self.assertEqual(results[0].json(), results[1].json())
        push.assert_called_once()

    def test_failed_push_does_not_mark_pushed_or_create_success_event(self):
        job_id = self.seed_job()
        with patch.object(routes, "commit_and_push", side_effect=GitPushError("Changes could not be pushed.")):
            self.assertEqual(self.client.post(f"/jobs/{job_id}/approve").status_code, 400)
        with self.factory() as db:
            self.assertIsNone(db.get(Job, job_id).pushed_at)
            self.assertIsNone(db.get(Job, job_id).commit_message)
            self.assertEqual(db.query(ActivityEvent).count(), 0)

    def test_other_users_cannot_approve_or_see_push_activity(self):
        job_id = self.seed_job(user_id=2)
        with self.factory() as db:
            record_activity(db, 2, "job_pushed", job_id)
            db.commit()
        with patch.object(routes, "commit_and_push") as push:
            self.assertEqual(self.client.post(f"/jobs/{job_id}/approve").status_code, 404)
            push.assert_not_called()
        self.assertEqual(self.client.get("/auth/activity").json(), [])

    def test_worker_completion_and_failure_persist_safe_summaries(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                job_id = self.seed_job(status="queued", updated_at=1)
                repo = str(Path("test-repository").resolve())
                target = str(Path(repo) / "src" / "App.jsx")
                with ExitStack() as mocks:
                    mocks.enter_context(patch.object(worker, "SessionLocal", self.factory))
                    mocks.enter_context(patch.object(worker, "token_for_job", return_value="unused-oauth-token"))
                    mocks.enter_context(patch.object(worker, "clone_repository", return_value=repo, side_effect=RuntimeError("private exception secret-token") if fails else None))
                    mocks.enter_context(patch.object(worker, "create_snapshot"))
                    mocks.enter_context(patch.object(worker, "detect_project_type", return_value="React"))
                    mocks.enter_context(patch.object(worker, "find_relevant_files", return_value=[target]))
                    mocks.enter_context(patch.object(worker, "read_files", return_value={"App.jsx": "old"}))
                    mocks.enter_context(patch.object(worker, "analyze_code", return_value="Analysis ready"))
                    mocks.enter_context(patch.object(worker, "generate_patch", return_value={"filename": "src/App.jsx", "updated_code": "new"}))
                    mocks.enter_context(patch.object(worker, "apply_patch"))
                    mocks.enter_context(patch.object(worker, "get_diff", return_value="-old\n+new"))
                    worker.run_job(job_id)
                with self.factory() as db:
                    saved = db.get(Job, job_id)
                    self.assertEqual(saved.status, "failed" if fails else "completed")
                    self.assertGreater(saved.updated_at, 1)
                    events = db.query(ActivityEvent).filter(ActivityEvent.job_id == job_id).all()
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0].kind, "job_failed" if fails else "job_completed")
                    self.assertNotIn("secret-token", events[0].message)
                    self.assertNotIn("secret-token", saved.ai_result)


if __name__ == "__main__":
    unittest.main()
