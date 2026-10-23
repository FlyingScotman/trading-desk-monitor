"""Restart of ``desk serve`` from the settings page («Перезапустить сервер», «Применить и перезапустить»).

The server cannot start itself again: it exits with the launcher's restart code (``DESK_RESTART_CODE``, the same
exit as a hot update without ``updates/pending.json``, ``desk.api.app.serve``) and ``app\\deskctl.py start``, which
waits on it, starts it again with the settings as they are now (address, port, HTTPS). Outside the launcher (a
checkout, ``desk serve`` by hand) there is nothing to start it again: the page says so.

A change of the network access (``desk.lan.enable_changes``: address, port, HTTPS, sign-in) is staged for the
launcher first, so that it cannot lock the owner out: ``<home>/restart/network.json`` names the previous values of
the keys it changes and the backups of the certificate files it replaces. The launcher starts the server with the
new settings; when that server does not answer ``/health/live`` on 127.0.0.1 (the port is taken, the certificate
does not load, any start failure), it writes the previous values back, restores the files and starts the server
as it was. Either way it writes the outcome to ``<home>/restart/last.json`` for the page (``kind``, ``ok``,
``message``, ``at``). The launcher owns both files once the server has exited; this module only writes the first.
Nothing here holds a password or a cell of a report: setting names, addresses, ports, file paths.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .settings import atomic_write_text

NETWORK = "network.json"
LAST = "last.json"


def restart_dir(home: Path) -> Path:
    return home / "restart"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def last(home: Path) -> dict[str, Any] | None:
    """The outcome of the last restart the launcher recorded (``last.json``), if any."""
    return _read(restart_dir(home) / LAST)


def pending(home: Path) -> dict[str, Any] | None:
    """A network change waiting for the launcher to confirm or roll it back (``network.json``), if any."""
    return _read(restart_dir(home) / NETWORK)


def backup_files(home: Path, paths: list[Path]) -> list[dict[str, str | None]]:
    """Copies of ``paths`` (the certificate and its key, before a new one is made) in ``<home>/restart/backup``;
    ``backup`` is None for a file that did not exist (nothing to restore)."""
    out: list[dict[str, str | None]] = []
    folder = restart_dir(home) / "backup"
    for p in paths:
        if p.is_file():
            folder.mkdir(parents=True, exist_ok=True)
            dst = folder / p.name
            shutil.copy2(p, dst)
            out.append({"target": str(p), "backup": str(dst)})
        else:
            out.append({"target": str(p), "backup": None})
    return out


def stage_network_change(home: Path, previous: dict[str, Any], *, files: list[dict[str, str | None]] | None = None,
                         expect: dict[str, Any] | None = None, by: str | None = None) -> Path:
    """Write ``network.json``: ``previous`` maps each settings.json key the change writes to its value before
    (None: the key was absent), ``files`` the backups to restore, ``expect`` the address the new server should
    listen on (shown in the outcome)."""
    doc = {"previous": previous, "files": files or [], "expect": expect or {}, "by": by,
           "at": datetime.now().isoformat(timespec="seconds")}
    return atomic_write_text(restart_dir(home) / NETWORK, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")


def drop_network_change(home: Path) -> None:
    """The change was not applied after all (the settings refused it): nothing for the launcher to check."""
    (restart_dir(home) / NETWORK).unlink(missing_ok=True)
