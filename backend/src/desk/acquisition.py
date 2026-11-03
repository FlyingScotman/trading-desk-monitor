"""One explicitly requested local acquisition, then the existing ingestion/EOD pipeline.

The file reader's offline authority does not change. Credentials exist only in the request/child stdin;
neither job status nor persistent settings contain them. Provider output is never a diagnostic log.

A provider that needs no login says so in its ``inspect`` result (``"login": "none"``). The service remembers that
answer for the inspected adapter fingerprint only, in memory; ``check``/``fetch`` without credentials are accepted
only while the current adapter fingerprint is that one. Absent, unknown or malformed ``login`` keeps the login.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from . import helper_proc, recompute, store
from .autopilot import CycleLock
from .helper_proc import MAX_REQUEST, MAX_RESPONSE  # noqa: F401 - the routes' and the tests' limits
from .inbox import scan
from .inbox.loader import AdapterHandle, fingerprint, load_adapter
from .inbox.timesem import MSK
from .settings import Settings

TIMEOUT_S = 1800.0
_HEX = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
_PROVIDER_CODES = frozenset((
    "invalid_request", "config_invalid", "source_disabled", "scope_report_missing", "scope_report_ambiguous",
    "scope_changed", "scope_book_missing", "scope_book_conflict", "scope_empty", "scope_too_large", "output_not_empty",
    "runtime_unavailable", "connection_failed", "auth_failed", "provider_failed", "cancelled",
    "export_failed", "export_invalid", "cache_invalid", "cache_invalidate_failed", "scope_invalid", "pack_failed",
))


class FlowError(Exception):
    def __init__(self, code: str, state: str = "blocked"):
        super().__init__(code)
        self.code, self.state = code, state


@dataclass(frozen=True)
class Provider:
    handle: AdapterHandle
    root: Path
    entry: Path
    source: str


def provider(st: Settings) -> Provider:
    h = load_adapter(st.private_adapter, str(st.private_adapter_config) if st.private_adapter_config else None,
                     st.private_adapter_sha256, inbox_dirs=[str(p) for p in st.inbox_dirs])
    if not h.ok:
        raise FlowError("adapter_unavailable")
    cap = h.describe.get("acquisition")
    if not isinstance(cap, dict) or not h.package_dir:
        raise FlowError("acquisition_unavailable")
    if type(cap.get("version")) is not int or cap["version"] != 1 or cap.get("dataset") != "trade_terms":
        raise FlowError("capability_invalid")
    rel = cap.get("entry")
    source = cap.get("source")
    decl = h.source(source) if isinstance(source, str) else None
    if not helper_proc.relative_entry(rel):
        raise FlowError("capability_invalid")
    if not decl or decl.get("dataset") != "trade_terms" or decl.get("scope") != ["book_id"] \
            or decl.get("covers") != "listed" or decl.get("completeness") != "full_snapshot" \
            or decl.get("time_basis") != "end_of_day":
        raise FlowError("capability_invalid")
    root = Path(h.package_dir).resolve()
    entry = helper_proc.contained(root, rel)
    if entry is None:
        raise FlowError("helper_unavailable")
    if fingerprint(str(root), h.config_dir) != h.fingerprint:
        raise FlowError("adapter_changed")
    if st.acquisition_python is None or not st.acquisition_python.is_file():
        raise FlowError("runtime_unavailable")
    return Provider(h, root, entry, source)


def parameters(fields: dict[str, list[str]]) -> dict[str, str | list[str]]:
    allowed = {"op", "date", "report", "report_sha256", "adapter_fingerprint", "book", "username", "password"}
    if set(fields) - allowed or any(len(v) != 1 for k, v in fields.items() if k != "book"):
        raise FlowError("invalid_request")
    p = {k: v if k == "book" else v[0] for k, v in fields.items()}
    if p.get("op") not in ("inspect", "check", "fetch"):
        raise FlowError("invalid_request")
    if any(not isinstance(v, str) or len(v) > 4096 for vs in fields.values() for v in vs):
        raise FlowError("invalid_request")
    try:
        if p["op"] != "check" and date.fromisoformat(p["date"]).isoformat() != p["date"]:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise FlowError("invalid_request") from None
    if p["op"] == "inspect" and (p.get("username") or p.get("password")):
        raise FlowError("invalid_request")
    if p["op"] in ("check", "fetch") and bool(p.get("username")) != bool(p.get("password")):
        raise FlowError("credentials_required")
    # Empty credentials are never forwarded: both absent means "no login", which ``Acquisition.start`` accepts only
    # when the last inspection of the current adapter said the provider needs none.
    if not p.get("username"):
        p.pop("username", None)
        p.pop("password", None)
    if p["op"] == "fetch":
        if not p.get("book") or len(p["book"]) > 100 or any(not b or len(b) > 200 for b in p["book"]):
            raise FlowError("scope_empty")
        if not _HEX.fullmatch(p.get("report", "")) or not _HEX.fullmatch(p.get("report_sha256", "")):
            raise FlowError("scope_report_missing")
        if not _HEX.fullmatch(p.get("adapter_fingerprint", "")):
            raise FlowError("scope_changed")
    return p


def login_mode(result: dict) -> str:
    """The inspect result's optional ``login``: ``"none"`` only when the provider says so exactly; absent (older
    adapters), the login-needed value or anything unknown/malformed means a login is required, never a failure."""
    return "none" if result.get("login") == "none" else "required"


class Acquisition:
    def __init__(self, settings_loader: Callable[[], Settings], store_url: str, *, timeout_s: float = TIMEOUT_S,
                 today: Callable[[], date] | None = None):
        """``today``: the Moscow date whose current runs a fetch also recomputes (TDM#547); a clock for tests."""
        self._load, self._url, self._timeout = settings_loader, store_url, timeout_s
        self._today = today or (lambda: datetime.now(MSK).date())
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: dict = {"state": "idle", "phase": "idle", "code": "idle"}
        # (adapter fingerprint, login mode) of the last successful inspection; never set from a client request
        self._login: tuple[str, str] | None = None

    def _login_for(self, fp: str) -> str:
        with self._lock:
            return self._login[1] if self._login is not None and self._login[0] == fp else "required"

    @staticmethod
    def _unchanged(p: Provider) -> bool:
        try:
            return fingerprint(str(p.root), p.handle.config_dir) == p.handle.fingerprint
        except Exception:  # noqa: BLE001 — unreadable now: not the inspected adapter
            return False

    def status(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._status))

    def _update(self, **fields) -> None:
        with self._lock:
            self._status.update(fields)

    def availability(self) -> dict:
        try:
            p = provider(self._load())
            return {"available": True, "code": "available", "source": p.source,
                    "name": p.handle.name, "version": p.handle.version, "login": self._login_for(p.handle.fingerprint)}
        except FlowError as e:
            return {"available": False, "code": e.code}
        except Exception:  # noqa: BLE001 — settings/plugin errors must never expose external diagnostics
            return {"available": False, "code": "settings_invalid"}

    def start(self, params: dict) -> dict:
        try:
            st = self._load()
            p = provider(st)
            if params.get("op") == "fetch" and params.get("adapter_fingerprint") != p.handle.fingerprint:
                raise FlowError("scope_changed")
            no_login = params.get("op") in ("check", "fetch") \
                and not (params.get("username") and params.get("password"))
            if no_login and (params.get("username") or params.get("password")):
                raise FlowError("credentials_required")
        except FlowError:
            params.clear()
            raise
        except Exception:  # noqa: BLE001 — no request data or external diagnostics in API errors
            params.clear()
            raise FlowError("settings_invalid") from None
        with self._lock:
            # Decided under the same lock as the start itself: an inspection that is still finishing cannot change
            # the answer between this check and the start (while it runs, the start is refused anyway).
            if no_login and self._login != (p.handle.fingerprint, "none"):
                params.clear()
                raise FlowError("credentials_required")
            if self._thread is not None and self._thread.is_alive():
                params.clear()
                raise FlowError("acquisition_busy")
            # one helper job at a time: not while a services operation or feed runs (08 §2.2); released by _work
            if not helper_proc.HELPER_LOCK.acquire(blocking=False):
                params.clear()
                raise FlowError("services_busy")
            if no_login:
                params.pop("username", None)  # never forward empty credentials
                params.pop("password", None)
            self._cancel.clear()
            self._status = {"id": uuid.uuid4().hex, "state": "running", "phase": "starting", "code": "starting",
                            "op": params["op"], "date": params.get("date"), "books": params.get("book", [])}
            self._thread = threading.Thread(target=self._work, args=(st, p, params), daemon=True,
                                            name="desk-acquisition")
            try:
                self._thread.start()
            except BaseException:
                helper_proc.HELPER_LOCK.release()
                raise
            return dict(self._status)

    def cancel(self) -> dict:
        with self._lock:
            if self._status.get("state") != "running":
                return dict(self._status)
            if self._status.get("phase") in ("ingesting", "valuating"):
                raise FlowError("cancel_after_commit_unavailable")
            self._cancel.set()
            self._status["code"] = "cancel_requested"
            return dict(self._status)

    def close(self) -> None:
        self._cancel.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=6)

    def _child(self, st: Settings, p: Provider, params: dict, out: Path) -> dict:
        if self._cancel.is_set():
            raise FlowError("cancelled", "cancelled")
        fields = {**{k: v for k, v in params.items() if k != "adapter_fingerprint"},
                  "adapter_root": str(p.root), "config_dir": p.handle.config_dir or "",
                  "inbox_dir": [str(d) for d in st.inbox_dirs], "output_dir": str(out)}
        params.pop("username", None)
        params.pop("password", None)
        try:
            result, returncode = helper_proc.run(st.acquisition_python, p.entry, fields, timeout=self._timeout,
                                                 cancel=self._cancel)
        except helper_proc.HelperFailure as f:
            raise {"cancelled": FlowError("cancelled", "cancelled"),
                   "timeout": FlowError("acquisition_timeout", "error"),
                   "request_invalid": FlowError("invalid_request"),
                   "runtime_unavailable": FlowError("runtime_unavailable", "error")}.get(
                f.code, FlowError("provider_response_invalid", "error")) from None
        finally:
            fields.clear()
        if result.get("state") not in ("complete", "partial"):
            code = result.get("code")
            if code not in _PROVIDER_CODES:
                raise FlowError("provider_response_invalid", "error")
            raise FlowError(code, "cancelled" if code == "cancelled" else "blocked"
                            if result.get("state") == "blocked" else "error")
        if returncode != 0:
            raise FlowError("provider_failed", "error")
        return result

    def _preconditions(self, st: Settings, params: dict) -> None:
        from .marketdata.bundle import SnapshotDir, SnapshotError

        day = date.fromisoformat(params["date"])
        try:
            SnapshotDir(st.snapshot_cache).market_id(day.isoformat())
        except SnapshotError:
            raise FlowError("market_missing") from None
        with store.connect(self._url) as con:
            inputs = store.positions_for(con, day)
            have = {b for _, b, *_ in inputs.rows}
            if not set(params["book"]) <= have:
                raise FlowError("positions_missing")

    def _pipeline(self, st: Settings, p: Provider, params: dict, path: Path, partial: bool) -> None:
        from .cli import eod_run

        day = date.fromisoformat(params["date"])
        books = frozenset(params["book"])
        with CycleLock(st.home) as held:
            if not held:
                raise FlowError("calculation_busy")
            self._preconditions(st, params)
            if self._cancel.is_set():
                raise FlowError("cancelled", "cancelled")
            if fingerprint(str(p.root), p.handle.config_dir) != p.handle.fingerprint:
                raise FlowError("adapter_changed")
            with self._lock:
                if self._cancel.is_set():
                    raise FlowError("cancelled", "cancelled")
                self._status.update(phase="ingesting", code="ingesting")
            with store.connect(self._url) as con:
                result = scan.ingest_produced(con, st, path, source=p.source, day=day, books=books, handle=p.handle)
                if result.busy:
                    raise FlowError("ingestion_busy")
                sha = scan._sha256(str(path))
                file_status = con.execute("SELECT status_id, status, adapter_fingerprint, source, report_date, valid_date"
                                          " FROM inbox_file_status WHERE sha256=%s ORDER BY status_id DESC LIMIT 1",
                                          (sha,)).fetchone()
                if not file_status or file_status[1:] != ("accepted", p.handle.fingerprint, p.source, day, day):
                    raise FlowError("snapshot_not_accepted")
                accepted = con.execute(
                    "SELECT scope, status, valid_date FROM inbox_unit WHERE status_id = %s",
                    (file_status[0],)).fetchall()
                if ({r[0].get("book_id") for r in accepted} != books
                        or any(r[1] != "accepted" or r[2] != day for r in accepted)):
                    raise FlowError("snapshot_not_accepted")
                terms = store.dataset_rows(con, "trade_terms", day)
                current = [u for u in terms.units if u["scope"].get("book_id") in books]
                wanted_sets = con.execute("SELECT dataset_set_id FROM inbox_unit WHERE status_id = %s"
                                          " AND status = 'accepted'", (file_status[0],)).fetchall()
                if ({u["set_id"] for u in current} != {r[0] for r in wanted_sets}
                        or any(u["source"] != p.source or u["valid_date"] != day for u in current)):
                    raise FlowError("snapshot_not_current")
                expected_hash = store.positions_for(con, day).private_inputs_hash
            self._update(phase="valuating", code="valuating")
            rid = eod_run(st, day.isoformat(), store_url=self._url, report_dir=None, out=lambda _: None)
            if rid is None:
                raise FlowError("positions_missing")
            with store.connect(self._url) as con:
                run = con.execute("SELECT status, private_inputs_hash, lineage FROM run WHERE run_id = %s",
                                  (rid,)).fetchone()
                if not run or run[0] != "complete" or run[1] != expected_hash \
                        or run[2].get("positions", {}).get("private_inputs_hash") != expected_hash \
                        or store.positions_for(con, day).private_inputs_hash != expected_hash:
                    raise FlowError("run_not_current", "error")
                block = con.execute("SELECT data FROM run_reference WHERE run_id = %s"
                                    " AND kind = 'derivatives_parallel'", (rid,)).fetchone()
                rows = [r for r in (block[0].get("rows", []) if block else []) if r.get("book_id") in books]
                counts = Counter(r["status"] for r in rows)
                reasons = Counter(r["reason"] for r in rows if r.get("reason"))
                incomplete = not rows or any(counts[k] for k in ("imported_only", "neither", "computed_invalid"))
            # TDM#547: the terms are a stock, in force on the later dates too: today's current runs (the morning or
            # intraday run, a preliminary close) that read other inputs are made again now, not at the next hub cut
            later = recompute.current_day(st, self._url, self._today(), out=lambda _: None) \
                if self._today() > day else {"runs": [], "failed": []}
            self._update(state="partial" if partial or incomplete else "complete", phase="complete", code="completed",
                         run_id=rid, private_inputs_hash=expected_hash,
                         valuation={"counts": dict(counts), "reasons": dict(reasons)},
                         recomputed=later["runs"], recompute_failed=later["failed"])

    def _work(self, st: Settings, p: Provider, params: dict) -> None:
        op = params["op"]
        try:
            if op == "fetch":
                self._preconditions(st, params)
            if self._cancel.is_set():
                raise FlowError("cancelled", "cancelled")
            out = st.home / "acquisition" / self.status()["id"]
            self._update(phase="acquiring", code="acquiring")
            result = self._child(st, p, params, out)
            if self._cancel.is_set():
                raise FlowError("cancelled", "cancelled")
            if op == "inspect":
                reports = result.get("reports")
                if not isinstance(reports, list) or len(reports) > 128 or result.get("date") != params["date"]:
                    raise FlowError("provider_response_invalid", "error")
                clean = []
                for r in reports:
                    if not isinstance(r, dict) or not _HEX.fullmatch(r.get("id", "")) \
                            or not _HEX.fullmatch(r.get("sha256", "")) or not isinstance(r.get("name"), str) \
                            or len(r["name"]) > 255 or not isinstance(r.get("books"), list) \
                            or len(r["books"]) > 100 or any(not isinstance(b, str) or not b or len(b) > 200 for b in r["books"]):
                        raise FlowError("provider_response_invalid", "error")
                    clean.append({k: r[k] for k in ("id", "sha256", "name", "books")})
                    policy = r.get("policy")
                    if not isinstance(policy, dict) or not isinstance(policy.get("status"), str) \
                            or not 1 <= len(policy["status"]) <= 64 \
                            or not isinstance(policy.get("excluded_products"), list) \
                            or len(policy["excluded_products"]) > 100 \
                            or any(not isinstance(v, str) or len(v) > 200 for v in policy["excluded_products"]) \
                            or not isinstance(policy.get("books"), dict) or set(policy["books"]) != set(r["books"]):
                        raise FlowError("provider_response_invalid", "error")
                    per_book = {}
                    for book, info in policy["books"].items():
                        if not isinstance(info, dict) or type(info.get("eligible")) is not int \
                                or not 0 <= info["eligible"] <= 10000000 \
                                or not isinstance(info.get("products"), list) or len(info["products"]) > 100 \
                                or any(not isinstance(v, str) or len(v) > 200 for v in info["products"]):
                            raise FlowError("provider_response_invalid", "error")
                        per_book[book] = {"eligible": info["eligible"], "products": info["products"]}
                    clean[-1]["policy"] = {"status": policy["status"],
                                           "excluded_products": policy["excluded_products"], "books": per_book}
                    # Optional (newer adapters): deals of books with no derivative in the positions that the scope
                    # leaves out. Only two counts are kept: those books are not in this window, so no codes, no sums.
                    # A malformed one is dropped: it is not worth failing the whole inspection.
                    skipped = policy.get("skipped")
                    if isinstance(skipped, dict) and type(skipped.get("trades")) is int \
                            and 0 <= skipped["trades"] <= 10000000 and isinstance(skipped.get("books"), dict) \
                            and len(skipped["books"]) <= 10000:
                        # ``n_books``: the full count (``books`` holds at most 100 codes); without it, the codes given.
                        n_books = skipped.get("n_books")
                        if type(n_books) is not int or not 0 <= n_books <= 10000000:
                            n_books = len(skipped["books"])
                        clean[-1]["policy"]["skipped"] = {"trades": skipped["trades"], "books": n_books}
                login = login_mode(result)
                if login == "none" and not self._unchanged(p):
                    login = "required"  # the adapter changed while inspected: the answer belongs to neither version
                with self._lock:
                    self._login = (p.handle.fingerprint, login)
                self._update(state="complete", phase="complete", code="scope_ready", reports=clean,
                             adapter_fingerprint=p.handle.fingerprint, login=login)
            elif op == "check":
                if result.get("code") != "connected":
                    raise FlowError("provider_response_invalid", "error")
                self._update(state="complete", phase="complete", code="connected")
            else:
                name = result.get("file", "")
                path = (out / name).resolve() if isinstance(name, str) else out
                if not isinstance(name, str) or not _NAME.fullmatch(name) or path.parent != out.resolve() \
                        or not path.is_file() or result.get("date") != params["date"] \
                        or result.get("books") != sorted(set(params["book"])) \
                        or scan._sha256(str(path)) != result.get("sha256"):
                    raise FlowError("provider_response_invalid", "error")
                self._pipeline(st, p, params, path, result["state"] == "partial")
        except FlowError as e:
            self._update(state=e.state, code=e.code)
        except Exception:  # noqa: BLE001 — a safe boundary around provider, database and valuation code
            phase = self.status().get("phase")
            self._update(state="error", code={"ingesting": "ingestion_failed", "valuating": "valuation_failed"}
                         .get(phase, "operation_failed"))
        finally:
            params.clear()
            helper_proc.HELPER_LOCK.release()
