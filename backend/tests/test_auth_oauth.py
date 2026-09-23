"""OAuth and ownership tests use isolated SQLite and mocked GitHub only."""

import base64
from contextlib import ExitStack
import hashlib
import json
import os
import secrets
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("SESSION_SECRET", "test-session-secret-with-at-least-32-characters")
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
from itsdangerous import TimestampSigner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth, main, routes
from app.database import Base, get_db
from app.models import AuthSession, Job, OAuthFlow, User

SECRET = "isolated-tests-session-secret-at-least-32-characters"
TOKEN = "oauth-access-secret-for-tests-only"
ORIGIN = "http://127.0.0.1:5173"
PROFILE = {"id": 2**40, "login": "octocat", "name": "Mona", "avatar_url": "https://avatars.githubusercontent.com/u/1"}
REPOSITORY = {"id": 42, "full_name": "octocat/project", "clone_url": "https://github.com/octocat/project.git",
              "default_branch": "main", "private": True, "permissions": {"push": True}, "archived": False}


class OAuthAndOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "SESSION_SECRET": SECRET, "APP_SECRET": "",
            "GITHUB_CLIENT_ID": "oauth-client-id", "GITHUB_CLIENT_SECRET": "oauth-client-secret",
            "GITHUB_CALLBACK_URL": ORIGIN + "/auth/github/callback", "FRONTEND_URL": ORIGIN,
            "CORS_ORIGINS": ORIGIN, "SESSION_HTTPS_ONLY": "false",
            "SESSION_SAME_SITE": "lax",
        }))
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.stack.callback(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.db_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

        def isolated_db():
            with self.db_factory() as db:
                yield db

        self.isolated_db = isolated_db
        self.stack.enter_context(patch.object(main, "initialize_database"))
        self.app = main.create_app()
        self.app.dependency_overrides[get_db] = isolated_db
        self.client = self.stack.enter_context(TestClient(self.app, base_url=ORIGIN, follow_redirects=False))

    def _session(self, client=None):
        cookie = (client or self.client).cookies.get(auth.SESSION_COOKIE)
        return json.loads(base64.b64decode(TimestampSigner(SECRET).unsign(cookie.encode())))

    def _begin_login(self):
        response = self.client.get("/auth/github/login")
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]

    def _finish_login(self, *, profile=None, token=TOKEN, expires_in=None):
        state = self._begin_login()
        data = {"access_token": token, "token_type": "bearer"}
        if expires_in is not None:
            data["expires_in"] = expires_in
        with patch.object(auth.httpx, "post") as exchange, patch.object(auth.httpx, "get") as user_request:
            exchange.return_value = httpx.Response(200, json=data, request=httpx.Request("POST", "https://github.com/login/oauth/access_token"))
            user_request.return_value = httpx.Response(200, json=profile or PROFILE)
            response = self.client.get("/auth/github/callback", params={"state": state, "code": "one-time-code"})
        return response, exchange, user_request

    def _seed_user(self, github_id=1, token=TOKEN):
        with self.db_factory() as db:
            user = User(github_id=github_id, username=f"user-{github_id}", access_token=auth.encrypt_token(token))
            db.add(user)
            db.commit()
            return user.id

    def _set_session(self, user_id, expires_at=None):
        opaque = secrets.token_urlsafe(48)
        with self.db_factory() as db:
            db.add(AuthSession(id=auth._digest(opaque), user_id=user_id, csrf_token="test-csrf",
                               expires_at=expires_at or int(time.time()) + 3600))
            db.commit()
        encoded = base64.b64encode(json.dumps({"session_id": opaque}).encode())
        signed = TimestampSigner(SECRET).sign(encoded).decode()
        self.client.cookies.clear()
        self.client.cookies.set(auth.SESSION_COOKIE, signed, domain="127.0.0.1", path="/")

    def _headers(self):
        return {"Origin": ORIGIN, "X-CSRF-Token": self.client.get("/auth/session").json()["csrf_token"]}

    def _seed_job(self, user_id=None):
        with self.db_factory() as db:
            job = Job(user_id=user_id, repo_url=REPOSITORY["clone_url"], task="Change title", status="completed",
                      diff="-old\n+new", workspace_path="isolated-workspace")
            db.add(job)
            db.commit()
            return job.id

    def test_login_redirect_has_browser_bound_state_and_s256_pkce(self):
        response = self.client.get("/auth/github/login")
        parsed = urlsplit(response.headers["location"])
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("https", "github.com", "/login/oauth/authorize"))
        query = parse_qs(parsed.query)
        with self.db_factory() as db:
            flow = db.get(OAuthFlow, auth._digest(query["state"][0]))
            verifier = auth.decrypt_token(flow.verifier_encrypted)
            self.assertNotEqual(flow.verifier_encrypted, verifier)
            self.assertEqual(flow.browser_nonce_hash, auth._digest(self.client.cookies.get(auth.FLOW_COOKIE)))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], [challenge])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(set(query["scope"][0].split()), {"repo", "read:user"})
        self.assertNotIn("oauth-client-secret", response.headers["location"])
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        self.assertIn("samesite=lax", response.headers["set-cookie"].lower())

    def test_wrong_missing_and_non_ascii_state_never_exchange_code(self):
        for state in ("", "wrong-state", "é-invalid-state"):
            with self.subTest(state=state):
                self._begin_login()
                with patch.object(auth.httpx, "post") as exchange:
                    response = self.client.get("/auth/github/callback", params={"code": "code", "state": state})
                self.assertEqual(response.status_code, 303)
                self.assertIn("auth_error=invalid_state", response.headers["location"])
                exchange.assert_not_called()

    def test_valid_state_cannot_be_used_from_another_browser(self):
        state = self._begin_login()
        self.client.cookies.clear()
        with patch.object(auth.httpx, "post") as exchange:
            response = self.client.get("/auth/github/callback", params={"code": "code", "state": state})
        self.assertIn("auth_error=invalid_state", response.headers["location"])
        exchange.assert_not_called()

    def test_expired_state_never_exchanges_code(self):
        state = self._begin_login()
        with self.db_factory() as db:
            db.get(OAuthFlow, auth._digest(state)).expires_at = int(time.time()) - 1
            db.commit()
        with patch.object(auth.httpx, "post") as exchange:
            response = self.client.get("/auth/github/callback", params={"code": "code", "state": state})
        self.assertIn("auth_error=invalid_state", response.headers["location"])
        exchange.assert_not_called()

    def test_authorization_denial_is_sanitized_and_consumes_state(self):
        state = self._begin_login()
        with patch.object(auth.httpx, "post") as exchange:
            response = self.client.get("/auth/github/callback", params={"state": state, "error": "access_denied", "error_description": "private-detail"})
        self.assertIn("auth_error=access_denied", response.headers["location"])
        self.assertNotIn("private-detail", str(response.headers))
        exchange.assert_not_called()
        with self.db_factory() as db:
            self.assertIsNone(db.get(OAuthFlow, auth._digest(state)))

    def test_token_exchange_failures_are_sanitized(self):
        state = self._begin_login()
        with patch.object(auth.httpx, "post", side_effect=httpx.RequestError("private-detail oauth-client-secret")):
            response = self.client.get("/auth/github/callback", params={"state": state, "code": "code"})
        self.assertIn("auth_error=sign_in_failed", response.headers["location"])
        self.assertNotIn("private-detail", str(response.headers))
        self.assertEqual(self.client.get("/auth/me").status_code, 401)

    def test_callback_encrypts_token_and_cookie_contains_only_opaque_identifier(self):
        response, exchange, profile_request = self._finish_login()
        self.assertEqual(response.headers["location"], ORIGIN + "/")
        self.assertEqual(set(self._session()), {"session_id"})
        self.assertTrue(exchange.call_args.kwargs["data"]["code_verifier"])
        self.assertEqual(profile_request.call_args.kwargs["headers"]["Authorization"], f"Bearer {TOKEN}")
        with self.db_factory() as db:
            user = db.query(User).one()
            current = db.query(AuthSession).one()
            self.assertEqual(user.github_id, PROFILE["id"])
            self.assertNotIn(TOKEN, user.access_token)
            self.assertEqual(auth.decrypt_token(user.access_token), TOKEN)
            self.assertEqual(current.id, auth._digest(self._session()["session_id"]))
        profile = self.client.get("/auth/session")
        self.assertTrue(profile.json()["authenticated"])
        self.assertEqual(profile.json()["user"]["login"], "octocat")
        self.assertNotIn(TOKEN, profile.text + json.dumps(self._session()))
        self.assertEqual(profile.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/auth/me").json()["username"], "octocat")

    def test_repeat_login_rotates_session_and_updates_same_account(self):
        self._finish_login()
        previous = self._session()["session_id"]
        self._finish_login(profile={**PROFILE, "login": "renamed"}, token="new-secret")
        self.assertNotEqual(previous, self._session()["session_id"])
        with self.db_factory() as db:
            user = db.query(User).one()
            self.assertEqual(user.username, "renamed")
            self.assertEqual(auth.decrypt_token(user.access_token), "new-secret")
            self.assertIsNone(db.get(AuthSession, auth._digest(previous)))

    def test_session_survives_new_app_instance_using_same_database_and_secret(self):
        self._finish_login()
        other = main.create_app()
        other.dependency_overrides[get_db] = self.isolated_db
        with TestClient(other, base_url=ORIGIN) as client:
            client.cookies.update(self.client.cookies)
            self.assertTrue(client.get("/auth/session").json()["authenticated"])

    def test_server_session_expiry_is_authoritative(self):
        self._set_session(self._seed_user(), expires_at=int(time.time()) - 1)
        self.assertFalse(self.client.get("/auth/session").json()["authenticated"])
        self.assertEqual(self.client.get("/jobs").status_code, 401)

    def test_session_lifetime_is_capped_by_github_token_expiry(self):
        self._finish_login(expires_in=120)
        with self.db_factory() as db:
            remaining = db.query(AuthSession).one().expires_at - int(time.time())
            self.assertGreater(remaining, 0)
            self.assertLessEqual(remaining, 120)

    def test_logout_revokes_database_session_and_clears_cookie(self):
        self._finish_login()
        response = self.client.post("/auth/logout", headers=self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.get("/auth/session").json()["authenticated"])
        with self.db_factory() as db:
            self.assertEqual(db.query(AuthSession).count(), 0)

    def test_all_private_routes_require_authentication(self):
        for method, path in (("GET", "/auth/me"), ("GET", "/auth/repositories"), ("GET", "/jobs"),
                             ("GET", "/jobs/42"), ("POST", "/jobs"), ("POST", "/jobs/42/approve")):
            with self.subTest(path=path):
                response = self.client.request(method, path, json={"repo_url": REPOSITORY["clone_url"], "task": "Change title"})
                self.assertEqual(response.status_code, 401)

    def test_missing_deleted_user_session_is_rejected(self):
        self._set_session(999)
        self.assertEqual(self.client.get("/auth/me").status_code, 401)

    def test_creation_checks_repository_and_uses_session_owner(self):
        user_id = self._seed_user()
        self._set_session(user_id)
        with patch.object(routes, "run_job") as run, patch.object(auth, "github_request", return_value=httpx.Response(200, json=REPOSITORY)) as github:
            response = self.client.post("/jobs", headers=self._headers(), json={"repo_url": REPOSITORY["clone_url"], "task": "Change title"})
        self.assertEqual(response.status_code, 202)
        run.assert_called_once_with(response.json()["id"])
        github.assert_called_once_with("/repos/octocat/project", TOKEN)
        with self.db_factory() as db:
            self.assertEqual(db.get(Job, response.json()["id"]).user_id, user_id)
        self.assertNotIn(TOKEN, response.text)

    def test_creation_rejects_repository_without_write_permission(self):
        self._set_session(self._seed_user())
        repository = {**REPOSITORY, "permissions": {"push": False}}
        with patch.object(auth, "github_request", return_value=httpx.Response(200, json=repository)), patch.object(routes, "run_job") as run:
            response = self.client.post("/jobs", headers=self._headers(), json={"repo_url": REPOSITORY["clone_url"], "task": "Change title"})
        self.assertEqual(response.status_code, 403)
        run.assert_not_called()
        with self.db_factory() as db:
            self.assertEqual(db.query(Job).count(), 0)

    def test_job_access_excludes_other_users_and_legacy_jobs(self):
        user_id, other_id = self._seed_user(1), self._seed_user(2)
        own, forbidden = self._seed_job(user_id), [self._seed_job(other_id), self._seed_job()]
        self._set_session(user_id)
        self.assertEqual([job["id"] for job in self.client.get("/jobs").json()], [own])
        self.assertEqual(self.client.get(f"/jobs/{own}").status_code, 200)
        with patch.object(routes, "commit_and_push") as push:
            for job_id in forbidden:
                self.assertEqual(self.client.get(f"/jobs/{job_id}").status_code, 404)
                self.assertEqual(self.client.post(f"/jobs/{job_id}/approve", headers=self._headers()).status_code, 404)
            push.assert_not_called()

    def test_approval_rechecks_permissions_and_decrypts_latest_token(self):
        user_id = self._seed_user(token="old-token")
        job_id = self._seed_job(user_id)
        self._set_session(user_id)
        with self.db_factory() as db:
            db.get(User, user_id).access_token = auth.encrypt_token("latest-token")
            db.commit()
        with patch.object(auth, "github_request", return_value=httpx.Response(200, json=REPOSITORY)) as github, patch.object(routes, "commit_and_push", return_value="Changes pushed successfully") as push:
            response = self.client.post(f"/jobs/{job_id}/approve", headers=self._headers())
        self.assertEqual(response.status_code, 200)
        github.assert_called_once_with("/repos/octocat/project", "latest-token")
        push.assert_called_once_with("isolated-workspace", "latest-token")
        self.assertNotIn("latest-token", response.text)

    def test_mutations_require_both_trusted_origin_and_csrf(self):
        user_id = self._seed_user()
        self._set_session(user_id)
        job_id = self._seed_job(user_id)
        headers = self._headers()
        bad_headers = [{"Origin": ORIGIN}, {**headers, "X-CSRF-Token": "wrong"},
                       {"Origin": ORIGIN, "X-CSRF-Token": "é".encode("utf-8")},
                       {**headers, "Origin": "https://attacker.example"}, {"X-CSRF-Token": headers["X-CSRF-Token"]}]
        with patch.object(routes, "commit_and_push") as push, patch.object(routes, "run_job") as run:
            for candidate in bad_headers:
                for path in ("/jobs", f"/jobs/{job_id}/approve", "/auth/logout"):
                    self.assertEqual(self.client.post(path, headers=candidate, json={"repo_url": REPOSITORY["clone_url"], "task": "Change title"}).status_code, 403)
            push.assert_not_called()
            run.assert_not_called()

    def test_repository_picker_filters_and_exposes_pagination_without_credentials(self):
        self._set_session(self._seed_user())
        payload = [REPOSITORY, {**REPOSITORY, "archived": True}, {**REPOSITORY, "permissions": {"push": False}}]
        result = httpx.Response(200, json=payload, headers={"Link": '<https://api.github.com/user/repos?page=2>; rel="next"'})
        with patch.object(auth, "github_request", return_value=result):
            response = self.client.get("/auth/repositories")
        self.assertEqual(len(response.json()["repositories"]), 1)
        self.assertTrue(response.json()["has_more"])
        self.assertEqual(response.json()["next_page"], 2)
        self.assertNotIn("permissions", response.json()["repositories"][0])
        self.assertNotIn(TOKEN, response.text)

    def test_jobs_reject_unsafe_urls_and_browser_credentials(self):
        self._set_session(self._seed_user())
        with patch.object(routes, "run_job") as run:
            for url in ("http://github.com/a/b", "https://example.com/a/b", "https://secret@github.com/a/b", "file:///tmp/repo"):
                self.assertEqual(self.client.post("/jobs", headers=self._headers(), json={"repo_url": url, "task": "Change"}).status_code, 422)
            self.assertEqual(self.client.post("/jobs", headers=self._headers(), json={"repo_url": REPOSITORY["clone_url"], "task": "Change", "github_token": "pasted"}).status_code, 422)
            run.assert_not_called()

    def test_plaintext_legacy_token_is_never_used_as_oauth_credential(self):
        with self.assertRaises(HTTPException) as error:
            auth.decrypt_token("legacy-plaintext-secret")
        self.assertEqual(error.exception.status_code, 401)
        self.assertNotIn("legacy-plaintext-secret", error.exception.detail)

    def test_missing_oauth_config_returns_honest_setup_state(self):
        with patch.dict(os.environ, {"GITHUB_CLIENT_ID": ""}):
            self.assertEqual(self.client.get("/auth/session").json(), {"authenticated": False, "configured": False, "user": None})
            self.assertIn("auth_error=not_configured", self.client.get("/auth/github/login").headers["location"])

    def test_session_route_is_public_and_documented_under_auth(self):
        response = self.client.get("/auth/session")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["authenticated"])
        self.assertIsNone(response.json()["user"])
        self.assertNotIn("csrf_token", response.json())
        schema = self.client.get("/openapi.json").json()
        operation = schema["paths"]["/auth/session"]["get"]
        self.assertEqual(operation["tags"], ["Auth"])
        self.assertIn("username", schema["components"]["schemas"]["SessionUser"]["properties"])
        for path, method in (("/auth/github/login", "get"), ("/auth/github/callback", "get"),
                             ("/auth/logout", "post")):
            self.assertIn(method, schema["paths"][path])

    def test_session_returns_requested_profile_from_signed_session(self):
        self._finish_login()
        response = self.client.get("/auth/session")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["user"]["id"], PROFILE["id"])
        self.assertEqual(payload["user"]["username"], PROFILE["login"])
        self.assertEqual(payload["user"]["avatar_url"], PROFILE["avatar_url"])
        self.assertNotIn("access_token", response.text)
        self.assertNotIn(TOKEN, response.text)
        self.client.cookies.clear()
        self.assertIsNone(self.client.get("/auth/session").json()["user"])

    def test_existing_session_does_not_depend_on_login_configuration(self):
        self._finish_login()
        with patch.dict(os.environ, {"GITHUB_CLIENT_ID": ""}):
            payload = self.client.get("/auth/session").json()
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["user"]["username"], PROFILE["login"])
        self.assertFalse(payload["configured"])

    def test_cookie_signing_uses_session_secret_when_app_secret_is_set(self):
        with patch.dict(os.environ, {"APP_SECRET": "separate-encryption-secret-at-least-32-characters"}):
            app = main.create_app()
            app.dependency_overrides[get_db] = self.isolated_db
            with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
                original = self.client
                self.client = client
                try:
                    self._finish_login()
                    self.assertIn("session_id", self._session())
                    self.assertTrue(client.get("/auth/session").json()["authenticated"])
                finally:
                    self.client = original

    def test_app_requires_persistent_secret(self):
        with patch.dict(os.environ, {"SESSION_SECRET": "too-short", "APP_SECRET": ""}):
            with self.assertRaisesRegex(RuntimeError, "at least 32"):
                main.create_app()

    def test_session_cookie_defaults_to_lax(self):
        with patch.dict(os.environ):
            os.environ.pop("SESSION_SAME_SITE", None)
            app = main.create_app()
            app.dependency_overrides[get_db] = self.isolated_db
            with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
                original_client = self.client
                self.client = client
                try:
                    response, _, _ = self._finish_login()
                finally:
                    self.client = original_client
        cookie = next(value for value in response.headers.get_list("set-cookie")
                      if value.startswith(auth.SESSION_COOKIE + "="))
        self.assertIn("samesite=lax", cookie.lower())
        self.assertIn("httponly", cookie.lower())

    def test_cross_site_session_cookie_is_secure_and_oauth_flow_stays_lax(self):
        api_origin = "https://repoagent.onrender.com"
        with patch.dict(os.environ, {
            "SESSION_SAME_SITE": "none", "SESSION_HTTPS_ONLY": "true",
            "GITHUB_CALLBACK_URL": api_origin + "/auth/github/callback",
        }):
            app = main.create_app()
            app.dependency_overrides[get_db] = self.isolated_db
            with TestClient(app, base_url=api_origin, follow_redirects=False) as client:
                original_client = self.client
                self.client = client
                try:
                    login = client.get("/auth/github/login")
                    flow_cookie = next(value for value in login.headers.get_list("set-cookie")
                                       if value.startswith(auth.FLOW_COOKIE + "="))
                    self.assertIn("samesite=lax", flow_cookie.lower())
                    self.assertIn("secure", flow_cookie.lower())
                    response, _, _ = self._finish_login()
                    session = client.get("/auth/session", headers={"Origin": ORIGIN})
                    self.assertTrue(session.json()["authenticated"])
                    self.assertEqual(session.headers["access-control-allow-origin"], ORIGIN)
                    self.assertEqual(session.headers["access-control-allow-credentials"], "true")
                finally:
                    self.client = original_client
        cookie = next(value for value in response.headers.get_list("set-cookie")
                      if value.startswith(auth.SESSION_COOKIE + "="))
        attributes = {value.strip().lower() for value in cookie.split(";")[1:]}
        self.assertTrue({"samesite=none", "secure", "httponly"}.issubset(attributes))
        self.assertNotIn(TOKEN, cookie)

    def test_cross_site_session_rejects_insecure_cookie_configuration(self):
        for callback in (ORIGIN + "/auth/github/callback", "https://repoagent.onrender.com/auth/github/callback"):
            with self.subTest(callback=callback), patch.dict(os.environ, {
                "SESSION_SAME_SITE": "none", "SESSION_HTTPS_ONLY": "false",
                "GITHUB_CALLBACK_URL": callback,
            }):
                with self.assertRaisesRegex(RuntimeError, "requires secure cookies"):
                    main.create_app()

    def test_session_rejects_invalid_same_site_configuration(self):
        for value in ("", "invalid", "strict"):
            with self.subTest(value=value), patch.dict(os.environ, {"SESSION_SAME_SITE": value}):
                with self.assertRaisesRegex(RuntimeError, "must be lax or none"):
                    main.create_app()


if __name__ == "__main__":
    unittest.main()
