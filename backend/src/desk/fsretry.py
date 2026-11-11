"""A file operation tried again while Windows says the file is in use.

On Windows a file that another reader has open with plain ``open()`` (the status screen, a request of the page, the
worker's own read, an antivirus, the indexer) can be neither replaced nor deleted for a moment: ``os.replace`` raises
``PermissionError [WinError 5]`` (or 32/33). The reader lets go within milliseconds, so the operation is simply tried
again: ``retry`` pauses 0.1 s, doubling up to 2 s, for the whole budget, then raises the last error. POSIX's
``EACCES`` / ``EBUSY`` count as "in use" too. Used by the adapter updater (``desk.updater.adapter``) and by the writers
of small state files (``replace`` below, ``settings.atomic_write_text``, ``adapter_services.write_state``,
``backfill.save_state``); it imports nothing of the program, so any module may use it.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

BUSY_WAIT_S = 15.0  # the budget of ``retry`` by default (installing an adapter release: many files, a slow antivirus)
REPLACE_WAIT_S = 2.0  # the budget of ``replace``: a small state file, a reader lets go within milliseconds
BUSY_WINERRORS = (5, 32, 33)  # access denied, sharing violation, lock violation

_sleep = time.sleep  # tests: no real pauses


def busy(e: OSError) -> bool:
    """Windows' "the file or folder is in use" (or POSIX's EACCES / EBUSY): worth another try a moment later."""
    return (isinstance(e, PermissionError) or getattr(e, "winerror", None) in BUSY_WINERRORS
            or e.errno in (errno.EACCES, errno.EBUSY))


def retry(fn: Callable[[], Any], budget_s: float | None = None, *, sleep: Callable[[float], Any] | None = None) -> Any:
    """``fn()``, again while it fails with ``busy``: pauses from 0.1 s doubling up to 2 s, ``budget_s``
    (``BUSY_WAIT_S``) in all; then the last error. ``sleep``: the pause (default ``time.sleep``)."""
    budget = BUSY_WAIT_S if budget_s is None else budget_s
    pause, waited = 0.1, 0.0
    while True:
        try:
            return fn()
        except OSError as e:
            if not busy(e) or waited >= budget:
                raise
            d = min(pause, budget - waited)
            (sleep or _sleep)(d)
            waited, pause = waited + d, min(pause * 2, 2.0)


def replace(src: str | Path, dst: str | Path, budget_s: float = REPLACE_WAIT_S) -> None:
    """``os.replace(src, dst)``, again while the target is in use (up to ``budget_s``). Not caught after that: the
    caller removes its temporary file."""
    retry(lambda: os.replace(src, dst), budget_s)
