"""Shared fixtures. ``store_url``: a fresh, migrated desk store database per test, on one embedded
PostgreSQL (pgserver) per test session. Tests that need it are skipped when pgserver is missing."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from desk.store import MIGRATIONS, migrate


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory) -> Iterator[object]:
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    yield server
    server.cleanup()


@pytest.fixture
def store_url(pg_server) -> str:
    name = "desk_" + uuid.uuid4().hex[:12]
    pg_server.psql(f"CREATE DATABASE {name};")
    url = pg_server.get_uri(name)
    assert migrate(url) == [f.stem for f in sorted(MIGRATIONS.glob("*.sql"))]
    return url
