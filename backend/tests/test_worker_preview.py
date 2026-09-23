"""Worker snapshot boundaries without network, model calls, or database writes."""
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")
from app import worker

class WorkerPreviewTests(unittest.TestCase):
    def run_fixture(self, patch_name="src/App.jsx", snapshot_failure=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "src" / "App.jsx"
            target.parent.mkdir()
            target.write_text("original", encoding="utf-8")
            job = SimpleNamespace(id=42, status="queued", workspace_id="preview-test", repo_url="https://github.com/example/project.git", task="Update src/App.jsx", ai_result=None, diff=None)
            db = MagicMock()
            db.query.return_value.filter.return_value.first.return_value = job
            seen = []
            def snapshot(repo, workspace, variant):
                seen.append((variant, target.read_text(encoding="utf-8")))
                if snapshot_failure: raise OSError("Preview disk unavailable")
                return True
            def apply(repo, generated):
                target.write_text(generated["updated_code"], encoding="utf-8")
            with (
                patch.object(worker, "SessionLocal", return_value=db),
                patch.object(worker, "token_for_job", return_value="not-a-real-token"),
                patch.object(worker, "clone_repository", return_value=str(root)),
                patch.object(worker, "create_snapshot", side_effect=snapshot),
                patch.object(worker, "detect_project_type", return_value="React/Node"),
                patch.object(worker, "find_relevant_files", return_value=[str(target)]),
                patch.object(worker, "read_files", return_value={"App.jsx": "original"}),
                patch.object(worker, "analyze_code", return_value="Ready"),
                patch.object(worker, "generate_patch", return_value={"filename":patch_name,"updated_code":"updated"}),
                patch.object(worker, "apply_patch", side_effect=apply) as apply_mock,
                patch.object(worker, "get_diff", return_value="-original\n+updated"),
            ):
                worker.run_job(42)
                return job, seen, apply_mock.call_count

    def test_snapshot_original_before_patch_and_updated_after(self):
        job, seen, calls = self.run_fixture()
        self.assertEqual(seen, [("before","original"),("after","updated")])
        self.assertEqual(job.status, "completed")
        self.assertEqual(calls, 1)

    def test_preview_failure_does_not_discard_generated_diff(self):
        job, seen, calls = self.run_fixture(snapshot_failure=True)
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.diff, "-original\n+updated")
        self.assertEqual(calls, 1)

    def test_model_cannot_redirect_patch_outside_selected_file(self):
        job, seen, calls = self.run_fixture(patch_name="../../outside.txt")
        self.assertEqual(job.status, "failed")
        self.assertEqual(calls, 0)
        self.assertEqual(seen, [("before","original")])

if __name__ == "__main__":
    unittest.main()
