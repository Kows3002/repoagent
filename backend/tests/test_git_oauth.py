"""Exercise direct pushes against local repositories; never contact GitHub."""

import base64
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git import Actor, Git, Repo

from app import git_push_service, git_service


REPO_URL = "https://github.com/example/private-project.git"
ACCESS_TOKEN = "oauth-test-secret"


class RepositoryURLTests(unittest.TestCase):
    def test_repository_urls_are_canonical(self):
        for value in (REPO_URL, REPO_URL[:-4], REPO_URL + "/"):
            with self.subTest(value=value):
                self.assertEqual(git_service.normalize_repo_url(value), REPO_URL)

    def test_credentials_and_non_github_destinations_are_rejected(self):
        for value in (
            "https://example.com/owner/repo",
            "https://github.com.example.com/owner/repo",
            "https://user:secret@github.com/owner/repo",
            "https://github.com@evil.example/owner/repo",
            "https://github.com:8443/owner/repo",
            "https://github.com/owner/repo/tree/main",
            "https://github.com/owner/..",
            "https://github.com/owner/%2e%2e",
            "https://github.com/owner/repo?x=1",
            "https://github.com/owner/repo#main",
            "https://github.com/owner/repo\n",
            "http://github.com/owner/repo",
            "git@github.com:owner/repo.git",
            "file:///tmp/repository",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                git_service.normalize_repo_url(value)


class LocalGitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.identity = Actor("Test Author", "test@example.invalid")
        self.remote = Repo.init(self.root / "remote.git", bare=True)
        self.addCleanup(self.remote.close)
        self.seed = Repo.init(self.root / "seed", initial_branch="trunk")
        self.addCleanup(self.seed.close)
        self.file = Path(self.seed.working_tree_dir) / "README.md"
        self.file.write_text("Original content\n", encoding="utf-8")
        self.seed.index.add(["README.md"])
        self.seed.index.commit("Initial commit", author=self.identity, committer=self.identity)
        self.seed.create_remote("origin", self.remote.git_dir)
        self.seed.git.push("origin", "trunk")
        self.remote.git.symbolic_ref("HEAD", "refs/heads/trunk")

    def clone(self):
        repo = Repo.clone_from(self.remote.git_dir, self.root / "job")
        self.addCleanup(repo.close)
        repo.remotes.origin.set_url(REPO_URL)
        return repo

    @contextmanager
    def local_transport(self, failure=None):
        """Keep real Git commits/push semantics, replacing only network transport."""
        original = Git._call_process
        calls = []

        def call(git, method, *args, **kwargs):
            if method in {"ls_remote", "push"}:
                self.assertIn(REPO_URL, args)
                calls.append((method, args, kwargs, dict(git.environment())))
                if failure and method == "push":
                    raise failure
                args = tuple(self.remote.git_dir if value == REPO_URL else value for value in args)
            return original(git, method, *args, **kwargs)

        with patch.object(Git, "_call_process", new=call):
            yield calls

    def assert_config_has_no_credentials(self, repo):
        config = (Path(repo.git_dir) / "config").read_text(encoding="utf-8")
        self.assertNotIn(ACCESS_TOKEN, config)
        self.assertNotIn(base64.b64encode(f"x-access-token:{ACCESS_TOKEN}".encode()).decode(), config)
        self.assertEqual(repo.remotes.origin.url, REPO_URL)

    def test_clone_supplies_oauth_in_environment_and_leaves_clean_remote(self):
        real_clone = Repo.clone_from

        def local_clone(url, path, **kwargs):
            self.assertEqual(url, REPO_URL)
            self.assertTrue(kwargs["single_branch"])
            self.assertEqual(kwargs["depth"], 1)
            self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
            repo = real_clone(self.remote.git_dir, path, **kwargs)
            repo.remotes.origin.set_url(url)
            return repo

        with (
            patch.object(git_service, "WORKSPACE", self.root / "workspaces"),
            patch.object(git_service.Repo, "clone_from", side_effect=local_clone) as clone,
        ):
            path = git_service.clone_repository("job-one", REPO_URL, ACCESS_TOKEN)
        clone.assert_called_once()
        env = clone.call_args.kwargs["env"]
        headers = [value for key, value in env.items() if key.startswith("GIT_CONFIG_VALUE_")]
        self.assertIn(
            "Authorization: Basic " + base64.b64encode(f"x-access-token:{ACCESS_TOKEN}".encode()).decode(),
            headers,
        )
        repo = Repo(path)
        self.addCleanup(repo.close)
        self.assertEqual(repo.active_branch.name, "trunk")
        self.assert_config_has_no_credentials(repo)

    def test_clone_failure_is_sanitized(self):
        with (
            patch.object(git_service, "WORKSPACE", self.root / "workspaces"),
            patch.object(git_service.Repo, "clone_from", side_effect=RuntimeError(f"Authentication failed {ACCESS_TOKEN}")),
            self.assertRaises(git_service.GitCloneError) as raised,
        ):
            git_service.clone_repository("job-one", REPO_URL, ACCESS_TOKEN)
        self.assertIn("Sign in with GitHub again", str(raised.exception))
        self.assertNotIn(ACCESS_TOKEN, str(raised.exception))

    def test_push_updates_only_existing_default_branch(self):
        repo = self.clone()
        initial_branches = [head.name for head in self.remote.heads]
        initial_local_branches = [head.name for head in repo.heads]
        (Path(repo.working_tree_dir) / "README.md").write_text("Reviewed change\n", encoding="utf-8")
        with self.local_transport() as calls:
            result = git_push_service.commit_and_push(repo.working_tree_dir, ACCESS_TOKEN)
        self.assertEqual(result, "Changes pushed successfully")
        self.assertEqual([head.name for head in self.remote.heads], initial_branches)
        self.assertEqual([head.name for head in repo.heads], initial_local_branches)
        self.assertEqual(self.remote.head.commit.message, "RepoAgent: Apply requested changes")
        self.assertEqual(self.remote.git.show("HEAD:README.md").rstrip("\r\n"), "Reviewed change")
        push_call = next(call for call in calls if call[0] == "push")
        self.assertEqual(push_call[1], (REPO_URL, "HEAD:refs/heads/trunk"))
        self.assertEqual(push_call[2], {})
        self.assertIn("Authorization: Basic", " ".join(push_call[3].values()))
        self.assert_config_has_no_credentials(repo)

    def test_concurrent_upstream_commit_rejects_without_force(self):
        repo = self.clone()
        (Path(repo.working_tree_dir) / "README.md").write_text("Reviewed change\n", encoding="utf-8")
        self.file.write_text("Concurrent upstream change\n", encoding="utf-8")
        self.seed.index.add(["README.md"])
        upstream = self.seed.index.commit("Concurrent edit", author=self.identity, committer=self.identity)
        self.seed.git.push("origin", "trunk")
        with self.local_transport(), self.assertRaises(git_push_service.GitPushError) as raised:
            git_push_service.commit_and_push(repo.working_tree_dir, ACCESS_TOKEN)
        self.assertIn("Generate and review", str(raised.exception))
        self.assertEqual(self.remote.head.commit.hexsha, upstream.hexsha)
        self.assertEqual([head.name for head in self.remote.heads], ["trunk"])
        self.assert_config_has_no_credentials(repo)

    def test_changed_default_branch_requires_new_review(self):
        repo = self.clone()
        self.remote.create_head("new-default", self.remote.head.commit)
        self.remote.git.symbolic_ref("HEAD", "refs/heads/new-default")
        (Path(repo.working_tree_dir) / "README.md").write_text("Reviewed change\n", encoding="utf-8")
        with self.local_transport() as calls, self.assertRaises(git_push_service.GitPushError) as raised:
            git_push_service.commit_and_push(repo.working_tree_dir, ACCESS_TOKEN)
        self.assertIn("default branch changed", str(raised.exception))
        self.assertFalse(any(call[0] == "push" for call in calls))
        self.assertEqual(repo.head.commit.message, "Initial commit")

    def test_push_failure_hides_credentials_and_preserves_clean_remote(self):
        repo = self.clone()
        (Path(repo.working_tree_dir) / "README.md").write_text("Reviewed change\n", encoding="utf-8")
        with (
            self.local_transport(RuntimeError(f"Authentication failed {ACCESS_TOKEN}")),
            self.assertRaises(git_push_service.GitPushError) as raised,
        ):
            git_push_service.commit_and_push(repo.working_tree_dir, ACCESS_TOKEN)
        self.assertIn("Sign in with GitHub again", str(raised.exception))
        self.assertNotIn(ACCESS_TOKEN, str(raised.exception))
        self.assert_config_has_no_credentials(repo)

    def test_non_github_remote_rejected_before_network_or_commit(self):
        repo = self.clone()
        repo.remotes.origin.set_url("https://example.invalid/owner/repo.git")
        (Path(repo.working_tree_dir) / "README.md").write_text("Reviewed change\n", encoding="utf-8")
        with self.local_transport() as calls, self.assertRaises(git_push_service.GitPushError):
            git_push_service.commit_and_push(repo.working_tree_dir, ACCESS_TOKEN)
        self.assertEqual(calls, [])
        self.assertEqual(repo.head.commit.message, "Initial commit")

    def test_configured_push_url_cannot_override_validated_destination(self):
        repo = self.clone()
        with repo.config_writer() as config:
            config.set_value('remote "origin"', "pushurl", "https://example.invalid/owner/repo.git")
        (Path(repo.working_tree_dir) / "README.md").write_text("Reviewed change\n", encoding="utf-8")
        with self.local_transport() as calls:
            git_push_service.commit_and_push(repo.working_tree_dir, ACCESS_TOKEN)
        push_call = next(call for call in calls if call[0] == "push")
        self.assertEqual(push_call[1], (REPO_URL, "HEAD:refs/heads/trunk"))
        self.assert_config_has_no_credentials(repo)


if __name__ == "__main__":
    unittest.main()
