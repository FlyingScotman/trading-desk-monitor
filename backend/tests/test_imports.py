"""Every ``desk`` module imports first in a fresh interpreter (no import cycle that only a lucky order hides), and the
release workflow's ``tools/release_assets.py`` starts (``.github/workflows/release.yml``)."""

from __future__ import annotations

import os
import pkgutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import desk

BACKEND = Path(__file__).resolve().parents[1]
SKIP = {"desk.__main__"}  # runs the CLI on import


def _modules() -> list[str]:
    return sorted(m.name for m in pkgutil.walk_packages(desk.__path__, "desk.") if m.name not in SKIP)


def test_every_desk_module_imports_first() -> None:
    names = _modules()
    assert "desk.settings" in names and "desk.rules.ocp" in names
    # one fresh interpreter per module: the module is the first desk import there (the interpreters run side by side,
    # several at a time: one after the other the check took more than a minute of the job)
    probe = "import importlib, sys; importlib.import_module(sys.argv[1])"

    def first_import(name: str) -> str | None:
        r = subprocess.run([sys.executable, "-B", "-c", probe, name], check=False, capture_output=True, text=True,
                           cwd=BACKEND, timeout=300)
        return f"{name}: {(r.stderr.strip().splitlines() or ['?'])[-1]}" if r.returncode else None

    with ThreadPoolExecutor(max_workers=min(os.cpu_count() or 2, 8)) as pool:
        bad = [x for x in pool.map(first_import, names) if x]
    assert not bad, "\n".join(bad)


def test_release_assets_tool_starts() -> None:
    tool = BACKEND / "tools" / "release_assets.py"
    if not tool.is_file():
        return
    r = subprocess.run([sys.executable, "-B", str(tool), "--help"], check=False, capture_output=True, text=True,
                       cwd=BACKEND, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
