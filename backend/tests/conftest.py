"""Shared fixtures. ``store_url``: a fresh, migrated desk store database per test, on one embedded
PostgreSQL (pgserver) per test session. Tests that need it are skipped when pgserver is missing.

Under pytest-xdist (``-n``, TDM#678; CI) a test session is one worker process: ``pg_server`` starts one embedded PostgreSQL
in the worker's own directory (``tmp_path_factory`` of the worker: ``popen-gw0`` and so on), so workers share no server, no
database and no folder; the locks of a process (``helper_proc.HELPER_LOCK``) are per worker as well. Tests marked ``serial``
(process pools of their own, measured time) are not run under xdist at all: see ``pytest_runtest_setup``."""

from __future__ import annotations

import os
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
def _worker_home(tmp_path_factory) -> Iterator[None]:
    """Under xdist each worker has a desk home of its own. Without one, the tests that do not set ``DESK_HOME`` share the
    checkout's ``.devdata`` (``%LOCALAPPDATA%\\DeskMonitor`` on Windows) and with it ``cycle.lock``: a CLI ``eod`` of one
    worker then finds the cycle of another one running (``calculation_busy``). A test that sets ``DESK_HOME`` itself still
    wins; ``settings.load`` with an environment of its own reads that one; a run without xdist keeps the default home."""
    if not os.environ.get("PYTEST_XDIST_WORKER") or os.environ.get("DESK_HOME"):
        yield
        return
    mp = pytest.MonkeyPatch()
    mp.setenv("DESK_HOME", str(tmp_path_factory.mktemp("desk_home")))
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


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item) -> None:
    """A ``serial`` test needs the machine to itself (it starts process pools of its own or measures time). Under xdist other
    workers run beside it, so it fails here, loudly, instead of being run in company or skipped: run ``-n N -m "not serial and
    not live and not bundle and not full_property"`` first and ``-m "serial and not live and not bundle and not full_property"``
    after it, without ``-n`` (CI does; a plain ``pytest`` runs everything one test after the other)."""
    if item.get_closest_marker("serial") is not None and os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail("a test marked `serial` was started by an xdist worker: deselect it with -m 'not serial and ...' in the "
                    "parallel run and run it without -n (see tests/conftest.py)", pytrace=False)
