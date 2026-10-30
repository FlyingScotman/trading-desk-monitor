"""The private adapter the updater installed (``desk.updater.adapter``), as the settings read it: no network, no
import of the adapter, nothing from ``desk.settings`` (which imports this module).

Layout in the data folder::

    <home>/adapter-releases/
        current.json            {"version", "package": "<version>/<name>", "previous", "installed_at", "source", "sha256"}
        <version>/<name>/       the adapter package (``__init__.py`` ...) of that release
        <version>/ADAPTER_RELEASE.json, README*   from the release zip

``installed`` answers the package folder ``current.json`` points to when it exists; ``package_version`` reads a
package's ``__version__ = "..."`` as text (the bundle's adapter is compared by it, never imported here).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

RELEASES_DIR = "adapter-releases"
CURRENT_FILE = "current.json"
# the release contract's version: 0.5.0, 1.2.3-rc1, 0.5.0.dev2
VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*(?:[.-][0-9A-Za-z]+)*")
_NUMS = re.compile(r"([0-9]+(?:\.[0-9]+)*)(.*)")
_INIT_VERSION = re.compile(r"""^__version__\s*(?::\s*str\s*)?=\s*["']([^"'\r\n]{1,100})["']""", re.MULTILINE)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")


def releases_dir(home: Path) -> Path:
    return home / RELEASES_DIR


def current_path(home: Path) -> Path:
    return releases_dir(home) / CURRENT_FILE


def valid_version(v: object) -> bool:
    return isinstance(v, str) and len(v) <= 100 and VERSION_RE.fullmatch(v) is not None


def package_rel(version: str, name: str) -> str:
    return f"{version}/{name}"


def safe_package(home: Path, rel: object) -> Path | None:
    """``<releases>/<version>/<name>`` for a ``current.json`` ``package`` value; None when it is anything else."""
    if not isinstance(rel, str):
        return None
    parts = rel.split("/")
    if len(parts) != 2 or not valid_version(parts[0]) or not _NAME.fullmatch(parts[1]):
        return None
    return releases_dir(home) / parts[0] / parts[1]


def read_current(home: Path) -> dict[str, Any] | None:
    """``current.json`` when it reads and names a version and a package of the right shape; else None."""
    try:
        doc = json.loads(current_path(home).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not valid_version(doc.get("version")) or safe_package(home, doc.get("package")) is None:
        return None
    return doc


def installed(home: Path) -> tuple[Path, str] | None:
    """(package folder, version) of the installed release when its folder holds a package; else None."""
    doc = read_current(home)
    if doc is None:
        return None
    pkg = safe_package(home, doc["package"])
    if pkg is None or not (pkg / "__init__.py").is_file():
        return None
    return pkg, doc["version"]


def package_version(pkg: Path) -> str | None:
    """``__version__`` of the package folder ``pkg`` read from its ``__init__.py`` as text; None when absent."""
    try:
        with open(pkg / "__init__.py", encoding="utf-8-sig", errors="replace") as f:
            text = f.read(256 * 1024)
    except OSError:
        return None
    m = _INIT_VERSION.search(text)
    return m.group(1).strip() if m else None


def compare(new: str | None, current: str | None) -> str:
    """``newer``, ``same``, ``older`` or ``other`` (no order: an unknown format, or the same numbers with different
    suffixes, ``0.5.0`` against ``0.5.0-rc1``)."""
    if new is None or current is None:
        return "other"
    if new == current:
        return "same"
    a, b = _NUMS.fullmatch(new), _NUMS.fullmatch(current)
    if a is None or b is None:
        return "other"
    x, y = [int(n) for n in a[1].split(".")], [int(n) for n in b[1].split(".")]
    width = max(len(x), len(y))
    x, y = x + [0] * (width - len(x)), y + [0] * (width - len(y))
    if x != y:
        return "newer" if x > y else "older"
    return "same" if a[2] == b[2] else "other"
