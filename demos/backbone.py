"""Shared by the MCP-backbone scripts in demos/: point MCP-backbone at its
disposable test database, and seed it with one workspace, one API key and
a small `film` table for the duration of a run.

Call use_test_database() before anything imports MCP-backbone's `app`
package: its settings are read once, at import."""

import contextlib
import datetime
import os
import sys
from collections.abc import AsyncGenerator
from pathlib import Path


def use_test_database(checkout: Path) -> None:
    """Sets MCP-backbone's environment to its test database, found as its
    tests/conftest.py finds it: APP_TEST_DATABASE_URL from the environment
    or the checkout's .env. The URL is never printed."""
    from cryptography.fernet import Fernet

    url = os.environ.get("APP_TEST_DATABASE_URL")
    env_file = checkout / ".env"
    if not url and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("APP_TEST_DATABASE_URL="):
                url = line.split("=", 1)[1].strip().strip("\"'")
    if not url:
        sys.exit("no APP_TEST_DATABASE_URL (environment or the checkout's .env)")
    os.environ["APP_DATABASE_URL"] = os.environ["APP_TARGET_DATABASE_URL"] = url
    os.environ.setdefault("APP_CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    os.environ["APP_ALLOW_PRIVATE_TARGETS"] = "true"


@contextlib.asynccontextmanager
async def seeded(
    query_rate_limit: int | None = None, target_url: str | None = None
) -> AsyncGenerator[str]:
    """Creates the tables, a workspace (with its own query rate limit, if
    given) and a `film` table, yields the workspace's API key, and drops
    everything afterwards, as the test suite does. With `target_url`, the
    workspace queries that database instead (for example the harness's
    local Pagila), and no `film` table is made."""
    from app.auth.models import ApiKey, Workspace
    from app.auth.security import generate_api_key, hash_api_key
    from app.db import Base, SessionLocal, dispose_target_engines, engine
    from app.query.models import DataSourceConnection
    from sqlalchemy import text

    if "test" not in (engine.url.database or ""):
        sys.exit("refusing: the database name doesn't contain 'test'")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with SessionLocal() as session:
            now = datetime.datetime.now(datetime.UTC)
            workspace = Workspace(name="demo", created_at=now, query_rate_limit=query_rate_limit)
            session.add(workspace)
            await session.flush()
            session.add(
                DataSourceConnection(
                    workspace_id=workspace.id,
                    connection_url=target_url or engine.url.render_as_string(hide_password=False),
                )
            )
            api_key = generate_api_key()
            session.add(
                ApiKey(
                    workspace_id=workspace.id,
                    key_hash=hash_api_key(api_key),
                    status="active",
                    created_at=now,
                )
            )
            if target_url is None:
                await session.execute(
                    text(
                        "create table film (id serial primary key, title text, rental_rate numeric)"
                    )
                )
                await session.execute(
                    text(
                        "insert into film (title, rental_rate) values "
                        "('Academy Dinosaur', 0.99), ('Ace Goldfinger', 4.99), "
                        "('Adaptation Holes', 2.99), ('Affair Prejudice', 2.99)"
                    )
                )
            await session.commit()
        yield api_key
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("drop table if exists film"))
            await conn.run_sync(Base.metadata.drop_all)
        await dispose_target_engines()
        await engine.dispose()
