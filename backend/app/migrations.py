"""Small, idempotent startup migration for the OAuth schema.

Existing jobs keep their IDs, generated output, diffs and workspaces. Jobs from
before OAuth have no trustworthy owner, so their user_id stays NULL and the API
does not expose them to any signed-in user.
"""

from sqlalchemy import inspect, text

from app.database import Base
from app import models  # Register all tables before create_all.


def initialize_database(engine):
    """Create current tables and migrate an existing PostgreSQL/SQLite database.

    Do not suppress failures: the application must not start with a partially
    migrated schema. Migrations are additive and preserve historical job data.
    """
    dialect = engine.dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("RepoAgent supports PostgreSQL or SQLite databases.")

    with engine.begin() as connection:
        if dialect == "postgresql":
            # Serialize schema changes when multiple API processes start.
            connection.execute(text("SELECT pg_advisory_xact_lock(742918351)"))
        else:
            # Python's legacy sqlite transaction mode otherwise leaves DDL
            # outside a real transaction. Also serializes concurrent startups.
            connection.exec_driver_sql("BEGIN IMMEDIATE")

        Base.metadata.create_all(bind=connection)
        columns = {column["name"] for column in inspect(connection).get_columns("jobs")}

        if "user_id" not in columns:
            connection.execute(text(
                "ALTER TABLE jobs ADD COLUMN user_id INTEGER REFERENCES users(id)"
            ))

        # Leave legacy columns untouched. They are not mapped by the ORM and
        # cannot be returned by the authenticated API; historical jobs have no
        # established owner. Data cleanup requires a separate explicit migration.

        for index in models.Job.__table__.indexes:
            if "user_id" in index.columns.keys():
                index.create(bind=connection, checkfirst=True)
