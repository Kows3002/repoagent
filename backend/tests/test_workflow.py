"""Workflow tests without database writes, network access, or git pushes.

Run from backend: python -m unittest discover -s tests -v
"""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

# Client construction requires a key, but all model requests are mocked.
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import routes, worker
from app.schemas import JobCreate
from app.git_push_service import DEFAULT_COMMIT_MESSAGE, PushResult


class RoutesTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.user = SimpleNamespace(id=7, access_token="secret-token")
        self.current = SimpleNamespace(user_id=7, user=self.user)
        self.job = SimpleNamespace(
            id=42, status="completed", diff="-old\n+new", workspace_path="test-workspace",
            user_id=7, user=self.user, repo_url="https://github.com/example/project.git",
            task="Change the title", ai_result=None,
        )
        self.db.query.return_value.filter.return_value.first.return_value = self.job
        permissions = patch.object(routes, "repository_for_job", return_value={"clone_url": self.job.repo_url})
        permissions.start()
        self.addCleanup(permissions.stop)
        token = patch.object(routes, "token_for_job", return_value="secret-token")
        token.start()
        self.addCleanup(token.stop)

    def test_creation_returns_queued_before_processing(self):
        tasks = BackgroundTasks()
        self.db.refresh.side_effect = lambda job: setattr(job, "id", 42)
        request = JobCreate(repo_url=self.job.repo_url, task="Change title")
        with patch.object(routes, "run_job") as run:
            result = routes.create_job(request, tasks, self.db, self.current)
            self.assertEqual(result.status, "queued")
            self.assertEqual(result.user_id, self.user.id)
            run.assert_not_called()
            self.assertEqual(len(tasks.tasks), 1)
            asyncio.run(tasks())
            run.assert_called_once_with(42)

    def test_response_excludes_token(self):
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.get_db] = lambda: self.db
        app.dependency_overrides[routes.get_current_session] = lambda: self.current
        with TestClient(app) as client:
            response = client.get("/jobs/42")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access_token", response.json())
        self.assertNotIn("secret-token", response.text)

    def test_approval_requires_completed_job(self):
        self.job.status = "generating"
        with patch.object(routes, "commit_and_push") as push:
            with self.assertRaises(HTTPException) as raised:
                routes.approve_job(42, self.db, self.current)
            self.assertEqual(raised.exception.status_code, 409)
            push.assert_not_called()

    def test_approval_rejects_empty_diff(self):
        self.job.diff = "  \n"
        with patch.object(routes, "commit_and_push") as push:
            with self.assertRaises(HTTPException) as raised:
                routes.approve_job(42, self.db, self.current)
            self.assertIn("Nothing changed", raised.exception.detail)
            push.assert_not_called()

    def test_approval_reports_commit_message(self):
        with patch.object(routes, "commit_and_push", return_value=PushResult(DEFAULT_COMMIT_MESSAGE)) as push:
            result = routes.approve_job(42, self.db, self.current)
        push.assert_called_once_with("test-workspace", self.user.access_token, DEFAULT_COMMIT_MESSAGE)
        self.assertEqual(result["commit_message"], DEFAULT_COMMIT_MESSAGE)

    def approval_client(self):
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.get_db] = lambda: self.db
        app.dependency_overrides[routes.require_csrf] = lambda: self.current
        return TestClient(app)

    def test_approval_without_body_or_message_uses_default(self):
        with self.approval_client() as client:
            for options in ({}, {"json": {}}, {"json": None}):
                with self.subTest(options=options), patch.object(routes, "commit_and_push", return_value=PushResult(DEFAULT_COMMIT_MESSAGE)) as push:
                    response = client.post("/jobs/42/approve", **options)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["commit_message"], DEFAULT_COMMIT_MESSAGE)
                    push.assert_called_once_with("test-workspace", self.user.access_token, DEFAULT_COMMIT_MESSAGE)

    def test_approval_trims_and_passes_custom_message(self):
        custom_message = "Fix login title"
        with self.approval_client() as client, patch.object(routes, "commit_and_push", return_value=PushResult(custom_message)) as push:
            response = client.post("/jobs/42/approve", json={"commit_message": "  " + custom_message + "  "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "Changes pushed successfully", "job_id": 42, "commit_message": custom_message})
        push.assert_called_once_with("test-workspace", self.user.access_token, custom_message)

    def test_approval_reports_actual_message_when_retry_requests_another(self):
        with self.approval_client() as client, patch.object(routes, "commit_and_push", return_value=PushResult("Previously committed title")) as push:
            response = client.post("/jobs/42/approve", json={"commit_message": "Different retry title"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["commit_message"], "Previously committed title")
        push.assert_called_once_with("test-workspace", self.user.access_token, "Different retry title")

    def test_approval_rejects_invalid_messages_before_push(self):
        invalid_messages = ["", "   ", "x" * 201, "line\nline", "line\r", "line\t", "line\0", "line\x7f", "line\x85", "line\u2028", "line\u2029", 123, None, []]
        with self.approval_client() as client, patch.object(routes, "commit_and_push") as push:
            for message in invalid_messages:
                with self.subTest(message=repr(message)):
                    response = client.post("/jobs/42/approve", json={"commit_message": message})
                    self.assertEqual(response.status_code, 422)
            response = client.post("/jobs/42/approve", json={"commit_message": "Fix title", "branch": "unreviewed"})
            self.assertEqual(response.status_code, 422)
            push.assert_not_called()

    def test_approval_accepts_maximum_length_unicode_message(self):
        message = "\u00e9" * 200
        with self.approval_client() as client, patch.object(routes, "commit_and_push", return_value=PushResult(message)) as push:
            response = client.post("/jobs/42/approve", json={"commit_message": message})
        self.assertEqual(response.status_code, 200)
        push.assert_called_once_with("test-workspace", self.user.access_token, message)

    def test_approval_never_returns_unexpected_exception_text(self):
        with patch.object(routes, "commit_and_push", side_effect=RuntimeError("secret-token in git URL")):
            with self.assertRaises(HTTPException) as raised:
                routes.approve_job(42, self.db, self.current)
        self.assertNotIn("secret-token", raised.exception.detail)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        token = patch.object(worker, "token_for_job", return_value="secret-token")
        token.start()
        self.addCleanup(token.stop)
        snapshot = patch.object(worker, "create_snapshot", return_value=True)
        snapshot.start()
        self.addCleanup(snapshot.stop)
        self.job = SimpleNamespace(
            id=42, status="queued", workspace_id="test", repo_url="test-repo",
            task="Update src/App.jsx", ai_result=None, diff=None,
            user=SimpleNamespace(id=7, access_token="secret-token"),
        )
        self.db = MagicMock()
        self.db.query.return_value.filter.return_value.first.return_value = self.job
        self.states = []
        self.db.commit.side_effect = lambda: self.states.append(self.job.status)

    def test_worker_persists_progress_and_diff(self):
        repo_path = str(Path("test-repository").resolve())
        target = str(Path(repo_path) / "src" / "App.jsx")
        with (
            patch.object(worker, "SessionLocal", return_value=self.db),
            patch.object(worker, "clone_repository", return_value=repo_path) as clone,
            patch.object(worker, "detect_project_type", return_value="React/Node"),
            patch.object(worker, "find_relevant_files", return_value=[target]),
            patch.object(worker, "read_files", return_value={"App.jsx": "old"}),
            patch.object(worker, "analyze_code", return_value="Analysis ready"),
            patch.object(worker, "generate_patch", return_value={"filename": "src/App.jsx", "updated_code": "new"}),
            patch.object(worker, "apply_patch"),
            patch.object(worker, "get_diff", return_value="-old\n+new"),
        ):
            worker.run_job(42)
        clone.assert_called_once_with("test", "test-repo", "secret-token")
        self.assertEqual(self.states[0], "analyzing")
        self.assertIn("generating", self.states)
        self.assertEqual(self.states[-1], "completed")
        self.assertEqual(self.job.diff, "-old\n+new")
        self.db.close.assert_called_once()

    def test_worker_missing_file_is_friendly_failure(self):
        with (
            patch.object(worker, "SessionLocal", return_value=self.db),
            patch.object(worker, "clone_repository", return_value="test-repository"),
            patch.object(worker, "detect_project_type", return_value="Unknown"),
            patch.object(worker, "find_relevant_files", return_value=[]),
            patch.object(worker, "analyze_code") as analyze,
        ):
            worker.run_job(42)
        self.assertEqual(self.job.status, "failed")
        self.assertIn("exact file path", self.job.ai_result)
        analyze.assert_not_called()

    def test_unknown_failure_does_not_expose_exception(self):
        with (
            patch.object(worker, "SessionLocal", return_value=self.db),
            patch.object(worker, "clone_repository", side_effect=RuntimeError("private secret-token")),
        ):
            worker.run_job(42)
        self.assertEqual(self.job.status, "failed")
        self.assertNotIn("secret-token", self.job.ai_result)


if __name__ == "__main__":
    unittest.main()
