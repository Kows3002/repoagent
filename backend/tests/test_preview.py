"""Preview tests use tiny fixtures and mocked Docker, never execute repositories."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from app import preview_service as previews


class PreviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.environment = patch.dict(os.environ, {"PREVIEW_STORAGE_PATH": str(self.root / "previews"), "APP_SECRET": "preview-test-secret-which-is-longer-than-32-characters"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def write(self, path, content):
        target = self.repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def snapshots(self, before="<h1>Before</h1>", after="<h1>After</h1>"):
        self.write("index.html", before)
        self.assertTrue(previews.create_snapshot(str(self.repository), "workspace-42", "before"))
        self.write("index.html", after)
        self.assertTrue(previews.create_snapshot(str(self.repository), "workspace-42", "after"))

    def test_original_and_updated_are_real_immutable_snapshots(self):
        self.snapshots()
        self.write("index.html", "<h1>Later change</h1>")
        previews.create_snapshot(str(self.repository), "workspace-42", "before")
        previews.build_previews("workspace-42", 42)
        before = previews.resolve_asset("workspace-42", "before", "index.html")
        after = previews.resolve_asset("workspace-42", "after", "index.html")
        self.assertEqual(before.read_text(), "<h1>Before</h1>")
        self.assertEqual(after.read_text(), "<h1>After</h1>")
        self.assertEqual(previews.get_preview_status("workspace-42")["status"], "ready")
        self.assertFalse((self.repository / "previews").exists())

    def test_snapshots_exclude_credentials_git_and_dependencies(self):
        self.write(".git/config", "token-in-remote-url")
        self.write(".env.production", "A_SECRET=example")
        self.write(".npmrc", "//registry:_authToken=secret")
        self.write("private.key", "private credential")
        self.write("node_modules/package/index.js", "unnecessary dependency")
        self.snapshots()
        snapshot = previews.workspace_root("workspace-42") / "snapshots" / "before"
        self.assertEqual(sorted(path.name for path in snapshot.iterdir()), ["index.html"])

    def test_static_assets_work_without_docker_and_unknown_paths_do_not_list_directories(self):
        self.write("styles.css", "body { color: blue }")
        self.write("src/server.py", "secret_internal = True")
        self.snapshots()
        with patch.object(previews, "_docker") as docker:
            previews.build_previews("workspace-42")
        docker.assert_not_called()
        self.assertIsNotNone(previews.resolve_asset("workspace-42", "after", "styles.css"))
        self.assertIsNone(previews.resolve_asset("workspace-42", "after", "src/server.py"))
        self.assertIsNone(previews.resolve_asset("workspace-42", "after", "missing.css", spa=True))
        self.assertIsNotNone(previews.resolve_asset("workspace-42", "after", "login", spa=True))

    def test_traversal_hidden_and_secret_paths_are_rejected(self):
        self.snapshots()
        previews.build_previews("workspace-42")
        for path in ("../status.json", "../../snapshots/before/index.html", ".git/config", ".env", "folder\\..\\index.html", "\x00.html", "secret.pem"):
            with self.subTest(path=path):
                self.assertIsNone(previews.resolve_asset("workspace-42", "after", path, spa=True))
        with self.assertRaises(previews.PreviewError):
            previews.workspace_root("../escape")

    def test_symlink_cannot_expose_a_file_outside_artifacts(self):
        self.snapshots()
        previews.build_previews("workspace-42")
        secret = self.root / "secret.html"
        secret.write_text("private")
        link = previews.workspace_root("workspace-42") / "artifacts/after/leak.html"
        try:
            link.symlink_to(secret)
        except OSError:
            self.skipTest("Symlink creation is not enabled on this Windows host")
        self.assertIsNone(previews.resolve_asset("workspace-42", "after", "leak.html"))

    def test_missing_old_snapshots_are_explained(self):
        previews.build_previews("older-job")
        result = previews.get_preview_status("older-job")
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("Generate a new change", result["message"])

    def test_unsupported_repository_never_claims_to_be_a_visual_preview(self):
        self.write("main.py", "print('not a web app')")
        previews.create_snapshot(str(self.repository), "workspace-42", "before")
        previews.create_snapshot(str(self.repository), "workspace-42", "after")
        previews.build_previews("workspace-42")
        result = previews.get_preview_status("workspace-42")
        self.assertEqual(result["status"], "unsupported")
        self.assertNotIn("before_url", result)

    def test_vite_requires_a_running_docker_daemon(self):
        self.write("package.json", json.dumps({"devDependencies": {"vite": "^6.0.0"}}))
        self.snapshots()
        with patch.object(previews, "_docker", side_effect=FileNotFoundError):
            previews.build_previews("workspace-42")
        result = previews.get_preview_status("workspace-42")
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("Start Docker", result["message"])

    def test_docker_build_is_offline_nonroot_and_installer_receives_no_source(self):
        self.write("package.json", json.dumps({"devDependencies": {"vite": "6.0.0"}, "scripts": {"postinstall": "malicious-command"}}))
        self.snapshots()
        calls = []

        def docker(arguments, timeout=30, check=True):
            calls.append(arguments)
            if "--network=none" in arguments:
                mount = next(part for part in arguments if part.startswith("type=bind,source=") and "target=/output" in part)
                output = Path(mount.removeprefix("type=bind,source=").split(",target=/output")[0])
                (output / "index.html").write_text("<h1>Real compiled output fixture</h1>")
            if "--network=bridge" in arguments:
                mount = next(part for part in arguments if "target=/manifest/package.json" in part)
                manifest = Path(mount.removeprefix("type=bind,source=").split(",target=/manifest")[0])
                self.assertNotIn("scripts", json.loads(manifest.read_text()))
                self.assertFalse(any("target=/source" in part for part in arguments))
                self.assertIn("--ignore-scripts", arguments[-1])

        with patch.object(previews, "_docker", side_effect=docker):
            previews.build_previews("workspace-42")
        self.assertEqual(previews.get_preview_status("workspace-42")["status"], "ready")
        builds = [call for call in calls if "--network=none" in call]
        self.assertEqual(len(builds), 2)
        for command in builds:
            for flag in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=768m", "--cpus=1", "1000:1000"):
                self.assertIn(flag, command)
            self.assertFalse(any("docker.sock" in part for part in command))
        self.assertEqual(len([call for call in calls if call[:2] == ["rm", "--force"]]), 4)

    def test_build_timeout_still_removes_containers_and_volume(self):
        self.write("package.json", json.dumps({"devDependencies": {"vite": "6.0.0"}}))
        self.snapshots()
        calls = []

        def docker(arguments, timeout=30, check=True):
            calls.append(arguments)
            if "--network=none" in arguments:
                raise subprocess.TimeoutExpired("docker", timeout)

        with patch.object(previews, "_docker", side_effect=docker):
            previews.build_previews("workspace-42")
        self.assertEqual(previews.get_preview_status("workspace-42")["status"], "failed")
        self.assertTrue(any(call[:2] == ["rm", "--force"] for call in calls))
        self.assertTrue(any(call[:3] == ["volume", "rm", "--force"] for call in calls))

    def test_non_registry_dependency_is_not_installed(self):
        self.write("package.json", json.dumps({"devDependencies": {"vite": "6.0.0"}, "dependencies": {"private": "git+ssh://internal/repository"}}))
        with self.assertRaises(previews.PreviewError):
            previews._dependency_manifest(self.repository)

    def test_framework_html_template_is_not_misrepresented_as_a_static_preview(self):
        self.write("package.json", json.dumps({"dependencies": {"react-scripts": "5.0.1"}}))
        self.write("public/index.html", '<div id="root"></div><link href="%PUBLIC_URL%/favicon.ico">')
        previews.create_snapshot(str(self.repository), "workspace-42", "before")
        previews.create_snapshot(str(self.repository), "workspace-42", "after")
        previews.build_previews("workspace-42")
        self.assertEqual(previews.get_preview_status("workspace-42")["status"], "unsupported")

    def test_preview_capabilities_are_scoped_and_expire(self):
        expires = int(time.time()) + 400
        token, expiry = previews.create_capability("workspace-42", 42, "before", "session-id-hash", expires)
        self.assertEqual(expiry, expires)
        claims = previews.read_capability(token)
        self.assertEqual(claims["job_id"], 42)
        self.assertEqual(claims["variant"], "before")
        self.assertEqual(claims["session_id"], "session-id-hash")
        other, _ = previews.create_capability("workspace-42", 42, "after", "session-id-hash", expires)
        self.assertNotEqual(token, other)
        with patch.object(previews.time, "time", return_value=expires + 1):
            self.assertIsNone(previews.read_capability(token))
        self.assertIsNone(previews.read_capability("../workspace-42/status"))

    def test_repeated_capability_requests_do_not_replace_immutable_claims(self):
        expires = int(time.time()) + 400
        first = previews.create_capability("workspace-42", 42, "before", "session-id-hash", expires)
        with patch.object(Path, "replace", side_effect=PermissionError("Windows file is being read")) as replace:
            repeated = previews.create_capability("workspace-42", 42, "before", "session-id-hash", expires)
        self.assertEqual(repeated, first)
        replace.assert_not_called()

    def test_concurrent_capability_publisher_is_reused_only_after_claims_match(self):
        expires = int(time.time()) + 400

        def concurrent_publish(temporary, target):
            # Model another request winning publication just before Windows
            # denies replacing the file while an iframe is reading it.
            target.write_text(temporary.read_text(encoding="utf-8"), encoding="utf-8")
            raise PermissionError("Windows file is being read")

        with patch.object(Path, "replace", autospec=True, side_effect=concurrent_publish):
            token, _ = previews.create_capability("workspace-42", 42, "before", "session-id-hash", expires)
        self.assertEqual(previews.read_capability(token)["session_id"], "session-id-hash")
        self.assertEqual(list((previews.preview_root() / "capabilities").glob("*.tmp")), [])

    def test_malformed_or_nonmatching_capability_records_are_never_reused(self):
        expires = int(time.time()) + 400
        token, _ = previews.create_capability("workspace-42", 42, "before", "session-id-hash", expires)
        path = previews.preview_root() / "capabilities" / f"{token}.json"
        wrong_claims = dict(previews.read_capability(token), session_id="another-session")
        for contents in ("malformed JSON", json.dumps(wrong_claims)):
            with self.subTest(contents=contents):
                path.write_text(contents, encoding="utf-8")
                with patch.object(Path, "replace", side_effect=PermissionError("secret filesystem path")):
                    with self.assertRaises(previews.PreviewError) as raised:
                        previews.create_capability("workspace-42", 42, "before", "session-id-hash", expires)
                self.assertNotIn("secret filesystem path", str(raised.exception))
                self.assertEqual(path.read_text(encoding="utf-8"), contents)

    def test_capability_write_failure_is_friendly_and_cleans_temporary_files(self):
        with patch.object(Path, "replace", side_effect=PermissionError("secret filesystem path")):
            with self.assertRaises(previews.PreviewError) as raised:
                previews.create_capability("workspace-42", 42, "before", "session-id-hash", int(time.time()) + 400)
        self.assertNotIn("secret filesystem path", str(raised.exception))
        self.assertEqual(list((previews.preview_root() / "capabilities").iterdir()), [])

    def test_interrupted_build_is_retryable_and_new_workspace_starts_idle(self):
        self.assertEqual(previews.get_preview_status("new")["status"], "idle")
        previews._state("new", "building", "Building")
        with patch.object(previews.time, "time", return_value=time.time() + 3600):
            self.assertEqual(previews.get_preview_status("new")["status"], "failed")


class PreviewRoutesTests(unittest.TestCase):
    """Exercise real ownership queries against an isolated in-memory database."""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from starlette.middleware.sessions import SessionMiddleware
        from app.database import Base
        from app.models import AuthSession, Job, User
        from app import preview_routes

        self.routes = preview_routes
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"PREVIEW_STORAGE_PATH": str(self.root / "previews"), "APP_SECRET": "preview-test-secret-which-is-longer-than-32-characters", "PREVIEW_DOMAIN": "localhost", "PREVIEW_PORT": "8000", "PREVIEW_SCHEME": "http", "FRONTEND_URL": "http://127.0.0.1:5173"})
        self.environment.start()
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions() as db:
            owner = User(id=7, github_id=700, username="owner", access_token="encrypted-test-fixture")
            other = User(id=8, github_id=800, username="other", access_token="encrypted-other-fixture")
            self.current = AuthSession(id="owner-session-hash", user=owner, csrf_token="owner-csrf", expires_at=int(time.time()) + 3600)
            self.other = AuthSession(id="other-session-hash", user=other, csrf_token="other-csrf", expires_at=int(time.time()) + 3600)
            db.add_all([owner, other, self.current, self.other, Job(id=42, workspace_id="workspace-42", user=owner, repo_url="https://github.com/owner/project", status="completed", task="Update title", diff="-Old\n+New")])
            db.commit()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        (self.repository / "main.js").write_text("document.body.dataset.loaded = 'yes'")
        (self.repository / "index.html").write_text('<h1>Original</h1><script type="module" src="/main.js"></script>')
        previews.create_snapshot(str(self.repository), "workspace-42", "before")
        (self.repository / "index.html").write_text('<h1>Updated</h1><script type="module" src="/main.js"></script>')
        previews.create_snapshot(str(self.repository), "workspace-42", "after")
        previews.build_previews("workspace-42")

        self.app = FastAPI()
        self.app.include_router(preview_routes.router)

        @self.app.get("/sentinel")
        def sentinel():
            return {"private_api": True}

        def dependency_db():
            with self.sessions() as db:
                yield db

        self.app.dependency_overrides[preview_routes.get_db] = dependency_db
        self.app.dependency_overrides[preview_routes.get_current_session] = lambda: self.current
        self.app.add_middleware(SessionMiddleware, secret_key="test-session-secret-at-least-thirty-two-characters")
        self.app.add_middleware(preview_routes.PreviewHostMiddleware)
        self.session_patch = patch.object(preview_routes, "SessionLocal", self.sessions)
        self.session_patch.start()
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8000")
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.session_patch.stop()
        self.engine.dispose()
        self.environment.stop()
        self.temporary.cleanup()

    def preview_urls(self):
        response = self.client.get("/jobs/42/preview")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_owner_gets_real_pages_and_opaque_origin_modules_load_with_cors(self):
        urls = self.preview_urls()
        self.assertEqual(urls["status"], "ready")
        self.assertNotEqual(urls["before_url"], urls["after_url"])
        before = self.client.get(urls["before_url"])
        after = self.client.get(urls["after_url"])
        self.assertIn("<h1>Original</h1>", before.text)
        self.assertIn("<h1>Updated</h1>", after.text)
        module = self.client.get(urls["after_url"] + "main.js", headers={"Origin": "null"})
        self.assertEqual(module.status_code, 200)
        self.assertTrue(module.headers["content-type"].startswith("text/javascript"))
        self.assertEqual(module.headers["access-control-allow-origin"], "*")
        self.assertNotIn("access-control-allow-credentials", module.headers)
        self.assertNotIn("set-cookie", module.headers)
        self.assertIn("connect-src 'none'", after.headers["content-security-policy"])
        self.assertIn("sandbox allow-scripts", after.headers["content-security-policy"])
        self.assertNotIn("allow-same-origin", after.headers["content-security-policy"])
        self.assertEqual(after.headers["referrer-policy"], "no-referrer")

    def test_repeated_metadata_reads_succeed_while_windows_denies_replacement(self):
        original = self.preview_urls()
        with patch.object(Path, "replace", side_effect=PermissionError("Windows file is being read")) as replace:
            repeated = self.preview_urls()
        self.assertEqual(repeated["before_url"], original["before_url"])
        self.assertEqual(repeated["after_url"], original["after_url"])
        replace.assert_not_called()

    def test_job_owner_check_protects_metadata_and_post(self):
        self.current = self.other
        self.assertEqual(self.client.get("/jobs/42/preview").status_code, 404)
        response = self.client.post("/jobs/42/preview", headers={"Origin": "http://127.0.0.1:5173", "X-CSRF-Token": "other-csrf"})
        self.assertEqual(response.status_code, 404)

    def test_asset_capability_cannot_be_minted_for_another_owner(self):
        token, _ = previews.create_capability("workspace-42", 42, "after", self.other.id, self.other.expires_at)
        response = self.client.get(self.routes.preview_origin(token) + "/")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Updated", response.text)

    def test_logout_revocation_disables_existing_asset_links(self):
        from app.models import AuthSession
        urls = self.preview_urls()
        with self.sessions() as db:
            db.delete(db.get(AuthSession, self.current.id))
            db.commit()
        self.assertEqual(self.client.get(urls["after_url"]).status_code, 404)

    def test_expired_session_disables_existing_asset_links(self):
        from app.models import AuthSession
        urls = self.preview_urls()
        with self.sessions() as db:
            current = db.get(AuthSession, self.current.id)
            current.expires_at = int(time.time()) - 1
            db.commit()
        self.assertEqual(self.client.get(urls["before_url"]).status_code, 404)

    def test_invalid_capability_and_preview_hosts_never_reach_api(self):
        urls = self.preview_urls()
        self.assertEqual(self.client.get(urls["after_url"] + "sentinel").status_code, 404)
        self.assertEqual(self.client.get("http://invalid.localhost:8000/sentinel").status_code, 404)
        self.assertEqual(self.client.get("http://" + "0" * 32 + ".localhost:8000/sentinel").status_code, 404)
        self.assertEqual(self.client.get("/sentinel").json(), {"private_api": True})
        self.assertEqual(self.client.post(urls["after_url"]).status_code, 405)

    def test_preview_creation_requires_csrf_and_completed_job(self):
        from app.models import Job
        self.assertEqual(self.client.post("/jobs/42/preview").status_code, 403)
        headers = {"Origin": "http://127.0.0.1:5173", "X-CSRF-Token": "owner-csrf"}
        self.assertEqual(self.client.post("/jobs/42/preview", headers=headers).status_code, 202)
        with self.sessions() as db:
            db.get(Job, 42).status = "generating"
            db.commit()
        with patch.object(previews, "build_previews") as build:
            self.assertEqual(self.client.post("/jobs/42/preview", headers=headers).status_code, 409)
        build.assert_not_called()

    def test_spa_routes_preserve_their_actual_root_path(self):
        previews._state("workspace-42", "ready", "Ready", kind="vite", project_root=".")
        urls = self.preview_urls()
        self.assertEqual(self.client.get(urls["after_url"] + "login").status_code, 200)
        self.assertIn("Updated", self.client.get(urls["after_url"] + "login").text)
        self.assertEqual(self.client.get(urls["after_url"] + "missing.js").status_code, 404)

    def test_asset_path_redirect_never_serves_html_on_api_origin(self):
        urls = self.preview_urls()
        from urllib.parse import urlsplit
        token = urlsplit(urls["after_url"]).hostname.split(".")[0]
        response = self.client.get(f"/previews/42/after/{token}/index.html", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], urls["after_url"] + "index.html")
        self.assertEqual(self.client.get(f"/previews/99/after/{token}/index.html", follow_redirects=False).status_code, 404)


if __name__ == "__main__":
    unittest.main()
