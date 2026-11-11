"""Shared fixtures. ``store_url``: a fresh, migrated desk store database per test, on one embedded
PostgreSQL (pgserver) per test session. Tests that need it are skipped when pgserver is missing."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

import desk.orchestrator as orch
from desk import parallel
from desk.store import MIGRATIONS, migrate

SHARED_CACHE = orch.ShockedCurveCache(max_entries=200_000)
"""One cache of shocked curves for the session (``orchestrator.new_cache``): the RUONIA OIS curve is rebootstrapped per
bucket shift (245 with the Hessian; ~0.08 s each on core 0.37, ~5-10 ms on 0.38) and the tests' contexts repeat the same curves; a shared cache never
changes a number (core keys it by curve content and shift)."""


@pytest.fixture(autouse=True, scope="session")
def _no_automatic_pool() -> Iterator[None]:
    """The history is computed in several processes by default (TDM#628: ``backfill_workers`` 0, ``parallel.auto_workers``);
    a test that does not ask for processes (``workers=2`` or a setting of its own) computes in its own, where its patches
    of the code reach. The tests of the automatic number call ``parallel.choose_workers``."""
    mp = pytest.MonkeyPatch()
    mp.setattr(parallel, "auto_workers", lambda: 1)
    yield
    mp.undo()


@pytest.fixture(autouse=True, scope="session")
def _shared_curve_cache() -> Iterator[None]:
    mp = pytest.MonkeyPatch()
    mp.setattr(orch, "new_cache", lambda: SHARED_CACHE)
    yield
    mp.undo()


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
