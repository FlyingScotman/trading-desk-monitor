"""Every ``desk`` module imports first in a fresh interpreter (no import cycle that only a lucky order hides), and the
release workflow's ``tools/release_assets.py`` starts (``.github/workflows/release.yml``)."""

from __future__ import annotations

import pkgutil
import subprocess
import sys
from pathlib import Path

import desk

BACKEND = Path(__file__).resolve().parents[1]
SKIP = {"desk.__main__"}  # runs the CLI on import


def _modules() -> list[str]:
    return sorted(m.name for m in pkgutil.walk_packages(desk.__path__, "desk.") if m.name not in SKIP)


def test_every_desk_module_imports_first() -> None:
    names = _modules()
    assert "desk.settings" in names and "desk.rules.ocp" in names
    # one fresh interpreter per module: the module is the first desk import there
    code = ("import importlib, subprocess, sys\n"
            "bad = []\n"
            "for n in sys.argv[1:]:\n"
            "    r = subprocess.run([sys.executable, '-c', 'import importlib, sys; importlib.import_module(sys.argv[1])', n],"
            " capture_output=True, text=True)\n"
            "    if r.returncode:\n"
            "        bad.append(n + ': ' + (r.stderr.strip().splitlines() or ['?'])[-1])\n"
            "print('\\n'.join(bad))\n")
    r = subprocess.run([sys.executable, "-c", code, *names], check=False, capture_output=True, text=True,
                       cwd=BACKEND, timeout=900)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", r.stdout


def test_release_assets_tool_starts() -> None:
    tool = BACKEND / "tools" / "release_assets.py"
    if not tool.is_file():
        return
    r = subprocess.run([sys.executable, str(tool), "--help"], check=False, capture_output=True, text=True,
                       cwd=BACKEND, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
