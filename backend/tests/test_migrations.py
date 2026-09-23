"""OAuth schema tests using isolated in-memory databases only."""

import unittest

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.migrations import initialize_database
from app.models import Job, User


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.engine.dispose()

    def test_fresh_schema_stores_owned_jobs_and_large_github_ids(self):
        initialize_database(self.engine)
        with Session(self.engine) as db:
            user = User(
                github_id=2**40, username="octocat",
                avatar_url="https://avatars.githubusercontent.com/u/1",
                access_token="oauth-secret",
            )
            job = Job(user=user, repo_url="https://github.com/octocat/project", task="Change title")
            db.add(job)
            db.commit()
            db.expire_all()
            restored = db.get(Job, job.id)
            self.assertEqual(restored.user.github_id, 2**40)
            self.assertEqual(restored.user.jobs, [restored])
            self.assertEqual(restored.user_id, user.id)

    def test_legacy_upgrade_preserves_job_data_without_mapping_old_credentials(self):
        with self.engine.begin() as connection:
            connection.execute(text("""
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY,
                    workspace_id VARCHAR UNIQUE,
                    workspace_path VARCHAR,
                    repo_url VARCHAR,
                    github_token VARCHAR,
                    task VARCHAR,
                    status VARCHAR,
                    ai_result TEXT,
                    diff TEXT
                )
            """))
            connection.execute(text("""
                INSERT INTO jobs VALUES (
                    42, 'workspace-42', '/work/project',
                    'https://github.com/octocat/project', 'old-secret',
                    'Change title', 'completed', 'Generated result', '-old\n+new'
                )
            """))

        initialize_database(self.engine)
        initialize_database(self.engine)

        columns = {column["name"] for column in inspect(self.engine).get_columns("jobs")}
        self.assertEqual(columns, set(Job.__table__.columns.keys()) | {"github_token"})
        self.assertNotIn("github_token", Job.__table__.columns.keys())
        self.assertTrue({"auth_sessions", "oauth_flows"}.issubset(inspect(self.engine).get_table_names()))
        with Session(self.engine) as db:
            job = db.get(Job, 42)
            self.assertEqual(job.workspace_id, "workspace-42")
            self.assertEqual(job.workspace_path, "/work/project")
            self.assertEqual(job.repo_url, "https://github.com/octocat/project")
            self.assertEqual(job.task, "Change title")
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.ai_result, "Generated result")
            self.assertEqual(job.diff, "-old\n+new")
            self.assertIsNone(job.user_id)
            self.assertIsNone(job.user)
            self.assertEqual(db.execute(text("SELECT github_token FROM jobs WHERE id = 42")).scalar_one(), "old-secret")

        foreign_keys = inspect(self.engine).get_foreign_keys("jobs")
        self.assertTrue(any(
            key["constrained_columns"] == ["user_id"]
            and key["referred_table"] == "users"
            for key in foreign_keys
        ))
        indexes = inspect(self.engine).get_indexes("jobs")
        self.assertTrue(any(index["column_names"] == ["user_id"] for index in indexes))

    def test_existing_owned_job_survives_repeated_startup(self):
        initialize_database(self.engine)
        with Session(self.engine) as db:
            db.add(Job(id=7, user=User(github_id=1, username="octocat", access_token="oauth-secret")))
            db.commit()
        initialize_database(self.engine)
        with Session(self.engine) as db:
            self.assertEqual(db.get(Job, 7).user.github_id, 1)

    def test_github_identity_is_unique(self):
        initialize_database(self.engine)
        with Session(self.engine) as db:
            db.add(User(github_id=1, username="old-name", access_token="one"))
            db.commit()
            db.add(User(github_id=1, username="new-name", access_token="two"))
            with self.assertRaises(IntegrityError):
                db.commit()

    def test_job_foreign_key_rejects_nonexistent_user(self):
        initialize_database(self.engine)
        with Session(self.engine) as db:
            db.add(Job(user_id=999, repo_url="https://github.com/octocat/project"))
            with self.assertRaises(IntegrityError):
                db.commit()


if __name__ == "__main__":
    unittest.main()
