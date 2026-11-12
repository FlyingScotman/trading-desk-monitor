"""The adapter's helper processes (08 §2.1 acquisition, §2.2 services): one contained Python script of the adapter
package, run with a restricted environment, one form-encoded request on stdin, one JSON object on stdout.

Shared by ``desk.acquisition`` and ``desk.adapter_services``: the entry containment, the process launch (own process
group, ``-I -B``, the environment allowlist, stderr discarded), the stdin/stdout limits, the timeout, cancellation
and the kill of the whole process tree. ``HELPER_LOCK``: one helper job of either kind at a time in this process.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

from .inbox import runner

MAX_REQUEST = 65536
MAX_RESPONSE = 262144
MAX_FIELDS = 256
MAX_VALUE = 4096
HELPER_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA")

HELPER_LOCK = threading.Lock()
"""Held by an acquisition job (``desk.acquisition``) and by every services operation (``desk.adapter_services``):
a services helper never runs at the same time as an acquisition job, nor two services helpers at once."""

_WAITERS = 0
_WAITERS_LOCK = threading.Lock()


def owner_wait_begin() -> None:
    """The owner's acquisition waits for ``HELPER_LOCK`` (TDM#673): background work that has not started yet stands
    aside (``owner_waiting``) until ``owner_wait_end``, so the owner is not overtaken by the next scheduled load."""
    global _WAITERS
    with _WAITERS_LOCK:
        _WAITERS += 1


def owner_wait_end() -> None:
    global _WAITERS
    with _WAITERS_LOCK:
        _WAITERS = max(0, _WAITERS - 1)


def owner_waiting() -> bool:
    return _WAITERS > 0


class HelperFailure(Exception):
    """``code``: ``cancelled``, ``timeout``, ``response_invalid`` (no, overflowing or non-object stdout, stdin not
    taken), ``request_invalid`` (over the request limits) or ``runtime_unavailable`` (the Python does not start)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def relative_entry(rel: object) -> bool:
    """The shape of a capability's ``entry``: a non-empty relative POSIX path without ``..`` or backslashes."""
    return isinstance(rel, str) and bool(rel) and "\\" not in rel and not PurePosixPath(rel).is_absolute() \
        and ".." not in PurePosixPath(rel).parts


def contained(root: Path, rel: object) -> Path | None:
    """``rel`` (the capability's ``entry``) inside the package ``root``: ``relative_entry`` that resolves (symlinks
    too) to an existing ``*.py`` file under ``root``; None otherwise."""
    if not relative_entry(rel):
        return None
    root = root.resolve()
    entry = (root / rel).resolve()
    if not entry.is_relative_to(root) or entry.suffix != ".py" or not entry.is_file():
        return None
    return entry


def encode(fields: dict[str, str | list[str]]) -> bytes:
    """The request as UTF-8 form bytes within the limits (HelperFailure ``request_invalid`` otherwise)."""
    n = sum(len(v) if isinstance(v, list) else 1 for v in fields.values())
    values = [x for v in fields.values() for x in (v if isinstance(v, list) else [v])]
    if n > MAX_FIELDS or any(not isinstance(x, str) or len(x) > MAX_VALUE for x in values):
        raise HelperFailure("request_invalid")
    payload = urlencode(fields, doseq=True).encode("utf-8")
    if len(payload) > MAX_REQUEST:
        raise HelperFailure("request_invalid")
    return payload


def run(python: Path, entry: Path, fields: dict[str, str | list[str]], *, timeout: float,
        cancel: threading.Event) -> tuple[dict, int]:
    """One helper call: (its JSON object, its exit code). ``fields`` is cleared once written (it may hold
    secrets). Cancellation (``cancel`` set), the timeout and any failure kill the process tree."""
    try:
        payload = encode(fields)
    finally:
        fields.clear()
    if cancel.is_set():
        raise HelperFailure("cancelled")
    proc = None
    chunks: list[bytes] = []
    overflow = threading.Event()
    write_failed = threading.Event()

    def send(data):
        try:
            proc.stdin.write(data)
            proc.stdin.close()
        except (OSError, ValueError):
            write_failed.set()

    def drain():
        data = proc.stdout.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            overflow.set()
        else:
            chunks.append(data)

    try:
        flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} \
            if os.name == "nt" else {"start_new_session": True}
        proc = subprocess.Popen([str(python), "-I", "-B", str(entry)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env={k: v for k, v in os.environ.items() if k.upper() in HELPER_ENV}, **flags)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        until = time.monotonic() + timeout
        writer = threading.Thread(target=send, args=(payload,), daemon=True)
        writer.start()
        del payload
        while proc.poll() is None:
            if cancel.wait(0.05):
                raise HelperFailure("cancelled")
            if time.monotonic() >= until:
                raise HelperFailure("timeout")
            if overflow.is_set():
                raise HelperFailure("response_invalid")
        reader.join(timeout=1)
        writer.join(timeout=1)
        if cancel.is_set():
            raise HelperFailure("cancelled")
        if reader.is_alive() or writer.is_alive() or write_failed.is_set() or overflow.is_set() or not chunks:
            raise HelperFailure("response_invalid")
        try:
            result = json.loads(chunks[0])
        except (ValueError, UnicodeError):
            raise HelperFailure("response_invalid") from None
        if not isinstance(result, dict):
            raise HelperFailure("response_invalid")
        return result, proc.returncode
    except OSError:
        raise HelperFailure("runtime_unavailable") from None
    finally:
        if proc is not None:
            runner._kill_tree(proc)
            try:
                proc.wait(timeout=5)  # bounded: a process that outlives its kill does not hold the caller
            except subprocess.TimeoutExpired:
                pass
            for stream in (proc.stdin, proc.stdout):
                if stream is not None and not stream.closed:
                    stream.close()
