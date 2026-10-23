"""Diagnostics export (R13): one JSON document about how the desk works on the owner's machine, without private data,
for the owner to hand to the agent (the settings page's «Диагностика», ``GET /api/v1/diagnostics``,
``desk diagnostics --out FILE``).

``build`` returns ``{"schema": "desk-diagnostics/1", ...}`` with these sections, each built on its own (one that
fails holds ``{"error": ...}`` and the rest is still there):

* ``app``: program version, build, logic version, core, Python and OS family, uptime, the ``DESK_*`` variables
  (a value only when it is a flag or a number, else ``set``);
* ``clock``: UTC, Moscow and local time, the Moscow date: runs are dated by it (R17);
* ``settings``: the effective settings, flags, numbers and enums as they are, every path, URL, name or secret only
  ``set``/``unset`` (a folder also whether it exists);
* ``lan``: network access without addresses: where it listens, sign-in, HTTPS, the certificate's name count and
  the names of this computer it lacks (counts), the firewall rule, ``desk serve``'s self-check of its addresses at
  start (``desk.lan``: status, time, detail per address);
* ``autopilot``: the thread's state and its last cycles with each step's status, duration and detail (scrubbed),
  the intraday step's window, next hub slot and what the hub answered; from ``<home>/logs/autopilot.log`` the
  cycles of any process (``desk cycle`` too) and per day each step's outcomes by kind;
* ``hub``: configured or not, a live check (reachable, token accepted, latest close and its age) and the hub's
  newest market snapshots of every cut, each marked whether it is cached (``hub=False`` skips the hub);
* ``snapshots``: the cached market snapshots (date, cut, id prefix, known/published times), each with its curve
  quote sets per ``source/dataset`` and their quote counts, the hub's own coverage and stale reasons, the expected
  curves missing, ZCYC/bond quote/fixing counts, and for the newest ones whether the RUONIA OIS and key-rate curves
  build from them and on which quote set (R14, R15);
* ``market_history`` (R24): whether the hub's history API serves the token or why not (``hub без API истории
  (версия < 0.2.0)``, ``токен без права read``), and per dataset the stored date range, row and bond counts, the
  queries complete, to fetch and failed, the bytes fetched (``hubhistory.status``);
* ``static``: the cached static snapshots: dates, schema, instrument counts by status, class, coupon type, floater
  formulas by benchmark, calendars;
* ``inbox``: the adapter (name, version), folders (count, exist), files by status, source and error code, units,
  issue codes, copies, file extensions, and ``reconcile`` (R22): rows of the reports, accepted and in the default
  run as totals, books by flag and age, loss codes, the last scan's counts and timings (no book ids); ``positions``: position sets per date (counts), today's book by asset class,
  unmapped bonds by reason, book ages;
* ``runs``: the last runs: id, date, type, cut, times, status, versions, snapshot id prefixes, position counts by
  valuation quality and reason code, discount curve, curves built, risk per curve (positions contributing, no
  amounts), floaters by index, coupon display counts (R16), PnL availability, basis and reason codes;
* ``logs``: per log file of ``<home>/logs`` the records by day, level and logger, the most frequent warning and
  error kinds and the last error lines, scrubbed; ``db``: migrations, table row counts, size.

Privacy is the point: no ISIN or SECID, no book, portfolio, trader or counterparty, no amount, price, notional or
PnL value, no file path, user or host name, no token. Sections hold dates, codes, counts and snapshot id prefixes;
free text (step details, errors, log lines) goes through ``Scrubber.text``, which replaces ISIN-like tokens, paths,
URLs, IP addresses, e-mails and amounts, and every sensitive value the settings and the store know (the hub token
and host, the proxy, the folders, user and computer names, book ids and names, traders, position and instrument ids,
report file names, the report's free ``x_*`` fields). A last pass over the whole document (``Scrubber.doc``) does
the same to every string and key.
"""

from __future__ import annotations

import contextlib
import dataclasses
import getpass
import json
import logging
import os
import platform
import re
import socket
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg

from . import VALUATION_VERSION, __version__, backup, store
from .inbox.timesem import MSK
from .settings import REPO_ROOT, Settings, SettingsError, env_overrides, load

log = logging.getLogger("desk.diagnostics")

SCHEMA = "desk-diagnostics/1"
MAX_RUNS = 20
MAX_CYCLES = 12            # the last autopilot cycles in full
CYCLE_DAYS = 3             # days of autopilot step outcomes by kind
LOG_DAYS = 7               # days of log counts
LOG_TAIL_BYTES = 4 << 20   # read at most this much of the end of each log file
MAX_LOG_LINES = 50         # the last warning and error lines
MAX_KINDS = 30             # most frequent kinds (signatures) listed
MAX_TEXT = 400             # a free-text line is cut here
MAX_LIST = 60              # lists copied from snapshot coverage are cut here
LOCAL_SNAPSHOT_DATES = 10  # cached closes (final, preliminary) of this many newest dates
LOCAL_INTRADAY = 8         # and this many newest intraday cuts
RATES_CHECKS = 4           # the newest market snapshots checked for building the RUONIA and key-rate curves (and
                           # listed with their quote sets and datasets in full; the others in short)
RATE_INDICES = ("RUONIA", "KEY_RATE", "RUONIA_INDEX")
REMOTE_ROWS = 60           # the hub's newest market snapshots listed
HUB_TIMEOUT_S = 15.0       # the live hub check waits at most this long per request
TOKEN_DAYS = 60            # position rows of this many days give the sensitive values to scrub

# ---- scrubbing --------------------------------------------------------------------------------------------------

ISIN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")
URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+")
_SEG = r"[^\s\"'<>|;,)]*"
WIN_PATH = re.compile(r"(?<![\w])[A-Za-z]:[\\/]" + _SEG + r"(?:\s[^\s\"'<>|;,)\\/]+[\\/]" + _SEG + r")*(?<![.:])")
UNC_PATH = re.compile(r"\\\\[^\s\"'<>|;,)]+(?<![.:])")
POSIX_PATH = re.compile(r"(?<![\w.:/-])/(?:home|root|tmp|var|opt|mnt|media|srv|etc|usr|Users|private|Volumes|data|"
                        r"run|nix|snap|workspace|app)(?=/|\b)[^\s\"'<>|;,)]*(?<![.:])")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
IPV4 = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?![\w.])")
AMOUNTS = (re.compile(r"(?<![\w.,])[-+−]?\d{1,3}(?:[,\u00a0\u202f ]\d{3})+(?:[.,]\d+)?(?![\w])"),  # 1,234,567.8
           re.compile(r"(?<![\w.,:-])[-+−]?\d{6,}(?:[.,]\d+)?(?![\w])"),                      # 1234567
           re.compile(r"(?<![\w.])[-+−]?\d+(?:[.,]\d+)?(?=\s?(?:₽|RUB\b|руб))"))             # 12.5 ₽
SAFE_WORDS = frozenset((
    "OFZ", "KBD", "KEY", "RUB", "USD", "EUR", "CNY", "EOD", "FX", "OTC", "BOND", "CORP", "OTHER", "RUONIA", "MOEX",
    "TEST", "INTRADAY", "REPLAY", "TRADING", "HTM", "AFS", "true", "false", "none", "null", "localhost", "desk",
    "Rates", "Credit", "ok", "error", "skipped"))
"""Values never scrubbed as tokens: generic words a book id or a desk may happen to equal (a book coded ``OFZ``
says nothing private, while scrubbing it would break the curve ``RUB OFZ`` in every line)."""


class Scrubber:
    """Replaces private values in text. ``tokens``: value -> placeholder (book ids and names, position and instrument
    ids, file names, paths, the hub token and host...), matched as substrings (a value shorter than 4 characters as
    a whole word); ``names``: user and computer names, matched as whole words in any case."""

    def __init__(self, tokens: dict[str, str] | None = None, names: Iterable[str] = ()) -> None:
        self.tokens: dict[str, str] = {}
        self.short: dict[str, str] = {}
        for value, label in (tokens or {}).items():
            self.add(value, label)
        names = sorted({n for n in names if n and len(n) >= 3 and n not in SAFE_WORDS}, key=len, reverse=True)
        self.names = re.compile(r"(?<![\w])(?:" + "|".join(map(re.escape, names)) + r")(?![\w])", re.IGNORECASE) \
            if names else None

    def add(self, value: Any, label: str) -> None:
        """A value to replace by ``label``. Shorter than 4 characters only a book id (as a whole word): a short
        value of anything else would hit ordinary words."""
        if not isinstance(value, str):
            return
        v = value.strip()
        if not v or v in SAFE_WORDS or len(v) < 2 or (v.isdigit() and len(v) < 6):
            return
        if len(v) >= 4:
            self.tokens.setdefault(v, label)
        elif label == "<book>":
            self.short.setdefault(v, label)
            self.__dict__.pop("_short_cache", None)

    def _apply_tokens(self, s: str, found: list[str] | None = None) -> str:
        for tok in (found if found is not None else self._present([s])):
            if tok in s:
                s = s.replace(tok, self.tokens[tok])
        if self.short and self._short_re is not None:
            s = self._short_re.sub(lambda m: self.short[m.group(0)], s)
        if self.names is not None:
            s = self.names.sub("<name>", s)
        return s

    @property
    def _short_re(self) -> re.Pattern | None:
        if not hasattr(self, "_short_cache"):
            keys = sorted(self.short, key=len, reverse=True)
            self._short_cache = re.compile(r"(?<![\w])(?:" + "|".join(map(re.escape, keys)) + r")(?![\w])") \
                if keys else None
        return self._short_cache

    def _present(self, strings: list[str]) -> list[str]:
        """The tokens that occur in ``strings``, longest first: a token is searched only when its first four
        characters occur somewhere (thousands of tokens against a document of a few hundred kB)."""
        blob = "\x00".join(strings)
        grams = {blob[i:i + 4] for i in range(max(0, len(blob) - 3))}
        hits = [t for t in self.tokens if t[:4] in grams and t in blob]
        return sorted(hits, key=len, reverse=True)

    @staticmethod
    def _patterns(s: str) -> str:
        s = URL.sub("<url>", s)
        s = WIN_PATH.sub("<path>", s)
        s = UNC_PATH.sub("<path>", s)
        s = POSIX_PATH.sub("<path>", s)
        s = EMAIL.sub("<email>", s)
        s = IPV4.sub("<ip>", s)
        return ISIN.sub("<ISIN>", s)

    def text(self, value: Any, limit: int = MAX_TEXT) -> str | None:
        """Free text made safe: private values, ISINs, paths, URLs, addresses and amounts replaced; cut at
        ``limit``."""
        if value is None:
            return None
        s = self._apply_tokens(self._patterns(str(value)))
        for rx in AMOUNTS:
            s = rx.sub("#", s)
        s = " ".join(s.split())
        return s if len(s) <= limit else s[:limit - 1] + "…"

    def doc(self, doc: Any) -> Any:
        """The last pass: every string and key of ``doc`` with the private values, ISINs, paths, URLs and
        addresses replaced (not the amounts: dates, ids and counts stay as they are)."""
        strings: list[str] = []

        def collect(x: Any) -> None:
            if isinstance(x, str):
                strings.append(x)
            elif isinstance(x, dict):
                for k, v in x.items():
                    strings.append(str(k))
                    collect(v)
            elif isinstance(x, (list, tuple)):
                for v in x:
                    collect(v)

        collect(doc)
        found = self._present(strings)

        def fix(s: str) -> str:
            return self._apply_tokens(self._patterns(s), found)

        def walk(x: Any) -> Any:
            if isinstance(x, str):
                return fix(x)
            if isinstance(x, dict):
                return {fix(str(k)): walk(v) for k, v in x.items()}
            if isinstance(x, (list, tuple)):
                return [walk(v) for v in x]
            return x

        return walk(doc)


DATE_TS = re.compile(r"\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:[.,]\d+)?)?(?:Z|[+-]\d\d:?\d\d)?)?")
SHORT_DATE = re.compile(r"(?<![\w.])\d\d\.\d\d(?:\.\d{2,4})?(?![\w.])")
HEX_ID = re.compile(r"\b[0-9a-f]{12,64}\b")
NUMBER = re.compile(r"[-+−]?\d+(?:[.,]\d+)?")


def signature(scrub: Scrubber, text: Any, limit: int = 160) -> str:
    """The kind of a message: scrubbed, with its dates, ids and numbers replaced, so equal kinds count together."""
    s = scrub.text(text, limit=1000) or ""
    s = DATE_TS.sub("<date>", s)
    s = SHORT_DATE.sub("<date>", s)
    s = HEX_ID.sub("<id>", s)
    s = NUMBER.sub("#", s)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _top(c: Counter, n: int = MAX_KINDS) -> dict[str, int]:
    return {str(k): v for k, v in c.most_common(n)}


def _cap(x: Any, depth: int = 0) -> Any:
    """A JSON value with its lists cut at ``MAX_LIST`` (snapshot coverage blocks)."""
    if isinstance(x, dict):
        return {str(k): _cap(v, depth + 1) for k, v in x.items()}
    if isinstance(x, list):
        out = [_cap(v, depth + 1) for v in x[:MAX_LIST]]
        return out + ([f"… {len(x) - MAX_LIST} more"] if len(x) > MAX_LIST else [])
    return x


def _iso(ts: datetime | date | None) -> str | None:
    if ts is None:
        return None
    return ts.isoformat(timespec="seconds") if isinstance(ts, datetime) else ts.isoformat()


def _id(sid: Any) -> str | None:
    return str(sid)[:12] if sid else None


# ---- sensitive values -------------------------------------------------------------------------------------------


def _path_tokens(p: Any) -> list[str]:
    if not p:
        return []
    s = str(p)
    out = [s, s.replace("\\", "/"), s.replace("/", "\\")]
    try:
        out.append(str(Path(s).resolve()))
    except (OSError, RuntimeError, ValueError):
        pass
    return out


def scrubber(settings: Settings, con: psycopg.Connection | None) -> Scrubber:
    """The scrubber of this machine: the settings' secrets, hosts and folders, the user and computer names, and from
    the store the book ids and names, traders, entities, position and instrument ids of the last ``TOKEN_DAYS``,
    report file names and the report's ``x_*`` values."""
    sc = Scrubber()
    for secret in (settings.hub_token, settings.proxy_password, settings.store_url, settings.private_adapter_sha256):
        sc.add(secret, "<secret>")
    for url in (settings.hub_url, settings.store_url, os.environ.get("HTTPS_PROXY"), os.environ.get("https_proxy"),
                os.environ.get("HTTP_PROXY"), os.environ.get("http_proxy")):
        if url:
            try:
                u = urlsplit(url if "://" in url else "http://" + url)
                sc.add(u.hostname, "<host>")
                sc.add(u.netloc, "<host>")
                sc.add(u.username, "<name>")
                sc.add(u.password, "<secret>")
            except ValueError:
                pass
            sc.add(url, "<url>")
    sc.add(settings.proxy_user, "<name>")
    if settings.proxy_user and "\\" in settings.proxy_user:
        for part in settings.proxy_user.split("\\"):
            sc.add(part, "<name>")
    for f in dataclasses.fields(settings):
        v = getattr(settings, f.name)
        for p in (v if isinstance(v, tuple) else (v,)):
            if isinstance(p, Path):
                for t in _path_tokens(p) + _path_tokens(str(p).removesuffix("**").rstrip("\\/")):
                    sc.add(t, "<path>")
    sc.add(settings.private_adapter, "<adapter>")
    for p in (settings.home, settings.logs_dir, settings.pg_dir, REPO_ROOT, Path.home(), Path.cwd(), sys.prefix,
              sys.executable, os.environ.get("DESK_STATIC_ROOT"), os.environ.get("DESK_BUNDLE_ROOT"),
              os.environ.get("DESK_DEFAULT_PRIVATE_ADAPTER"), os.environ.get("DESK_ENV_FILE"),
              os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA"), os.environ.get("USERPROFILE"),
              os.environ.get("TEMP"), os.environ.get("TMP")):
        for t in _path_tokens(p):
            sc.add(t, "<path>")
    names = set()
    for get in (getpass.getuser, socket.gethostname, socket.getfqdn):
        with contextlib.suppress(Exception):  # no user or host name: nothing to scrub
            names.add(get())
    names |= {os.environ.get(k) for k in ("USERNAME", "USER", "LOGNAME", "USERDOMAIN", "COMPUTERNAME",
                                          "HOSTNAME", "USERDNSDOMAIN")} - {None}
    names |= {n.split(".")[0] for n in names if n and "." in n}
    if con is not None:
        since = date.today() - timedelta(days=TOKEN_DAYS)
        queries = (
            ("<book>", ("SELECT DISTINCT book_id FROM position_set UNION SELECT book_id FROM book_rev"
                        " UNION SELECT DISTINCT book_id FROM run_position")),
            ("<book>", "SELECT DISTINCT unnest(ARRAY[name, trader, entity, ocp_to]) FROM book_rev"),
            ("<position>", ("SELECT DISTINCT unnest(ARRAY[i.position_id, i.secid, i.isin, i.source_instrument_id,"
                            " i.source_instrument_name, i.deal_id, i.regnum, i.moex_secid])"
                            " FROM position_input i JOIN position_set s USING (set_id) WHERE s.valid_date >= %(d)s")),
            ("<position>", ("SELECT DISTINCT e.value FROM position_input i JOIN position_set s USING (set_id),"
                            " jsonb_each_text(CASE WHEN jsonb_typeof(i.x) = 'object' THEN i.x ELSE '{}' END) e"
                            " WHERE s.valid_date >= %(d)s")),
            ("<position>", ("SELECT DISTINCT unnest(ARRAY[position_id, secid]) FROM run_position"
                            f" WHERE run_id IN (SELECT run_id FROM run ORDER BY run_id DESC LIMIT {MAX_RUNS})")),
            ("<file>", ("SELECT DISTINCT unnest(ARRAY[path, name]) FROM inbox_file"
                        " UNION SELECT DISTINCT unnest(ARRAY[path, archive_path]) FROM inbox_file_status"
                        " UNION SELECT DISTINCT path FROM inbox_unread UNION SELECT DISTINCT path FROM inbox_sighting"
                        " UNION SELECT DISTINCT source_name FROM position_set")),
        )
        for label, sql in queries:
            try:
                for (v,) in con.execute(sql, {"d": since}):
                    if not isinstance(v, str):
                        continue
                    sc.add(v, label)
                    if label == "<file>":  # a file's name and its name without the extension, wherever it stands
                        name = re.split(r"[\\/]", v)[-1]
                        sc.add(name, label)
                        if len(stem := name.rsplit(".", 1)[0]) >= 6:
                            sc.add(stem, label)
            except psycopg.Error as e:
                log.warning("diagnostics: sensitive values of %s not read: %s", label, type(e).__name__)
    sc.names = Scrubber(names=names).names
    return sc


# ---- sections ---------------------------------------------------------------------------------------------------

_FLAG_OR_NUMBER = re.compile(r"(?i)^(?:[01]|true|false|yes|no|on|off|[-+]?\d+(?:\.\d+)?)$")
_ENUM_FIELDS = ("hub_transport",)


def app_section(started_at: datetime | None, now: datetime) -> dict[str, Any]:
    from .publish.tdm_run import git_sha
    from .update import bundle_root, current_version

    try:
        from rub_fixed_income import __version__ as core
    except ImportError:
        core = None
    try:
        from . import logic_version
        logic = logic_version()
    except Exception:  # noqa: BLE001 - core missing: the rest is still worth reporting
        logic = None
    env = {}
    for k in sorted(os.environ):
        if k.startswith("DESK_"):
            v = os.environ[k].strip()
            env[k] = v if _FLAG_OR_NUMBER.match(v) else ("set" if v else "empty")
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
        env[k] = "set" if (os.environ.get(k) or os.environ.get(k.lower())) else "unset"
    return {
        "version": current_version(), "backend_version": __version__, "valuation_version": VALUATION_VERSION,
        "logic_version": logic, "core_version": core, "git_sha": git_sha(), "windows_bundle": bundle_root() is not None,
        "python": platform.python_version(), "python_implementation": platform.python_implementation(),
        "os": platform.system(), "os_release": platform.release(), "machine": platform.machine(),
        "cpu_count": os.cpu_count(), "pid_uptime_s": round((now - started_at).total_seconds()) if started_at else None,
        "started_at": _iso(started_at), "env": env,
    }


def clock_section(now: datetime) -> dict[str, Any]:
    local = now.astimezone()
    off = local.utcoffset()
    return {"utc": _iso(now), "msk": _iso(now.astimezone(MSK)), "msk_date": now.astimezone(MSK).date().isoformat(),
            "local": _iso(local.replace(tzinfo=None)),
            "local_utc_offset_h": off.total_seconds() / 3600 if off is not None else None,
            "local_tz": time.tzname[0] if time.tzname else None}


def settings_section(settings: Settings, error: str | None, scrub: Scrubber) -> dict[str, Any]:
    """Flags, numbers and enums as they are; every path, URL, name or secret only ``set``/``unset``; a folder also
    whether it exists (never its path)."""
    out: dict[str, Any] = {"error": scrub.text(error) if error else None}
    for f in dataclasses.fields(settings):
        v = getattr(settings, f.name)
        if isinstance(v, (bool, int, float)) or f.name in _ENUM_FIELDS:
            out[f.name] = v
        elif isinstance(v, Path):
            out[f.name] = {"set": True, "exists": _exists(v)}
        elif isinstance(v, tuple):
            out[f.name] = {"count": len(v), "exist": sum(_exists(Path(str(p).removesuffix("**").rstrip("\\/")))
                                                         for p in v),
                           "recursive": sum(str(p).endswith("**") for p in v)}
        else:
            out[f.name] = "set" if v else "unset"
    out["hub_url"] = {"set": bool(settings.hub_url),
                      "scheme": urlsplit(settings.hub_url).scheme if settings.hub_url else None}
    src = settings.hub_token_source or ""
    out["hub_token_source"] = ("env" if src.startswith("DESK_HUB_TOKEN") else "file" if src.startswith("file")
                               else "not set")
    out["store"] = "external" if settings.store_url else "embedded"
    out["private_adapter"] = ("reference" if not settings.private_adapter else "bundle default"
                              if settings.private_adapter_default else "custom")
    out["private_adapter_pinned"] = bool(settings.private_adapter_sha256)
    out["https_proxy"] = "set" if (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")) else "unset"
    try:
        out["env_overrides"] = env_overrides()
    except Exception:  # noqa: BLE001
        out["env_overrides"] = None
    return out


def _exists(p: Path) -> bool:
    try:
        return p.exists()
    except OSError:
        return False


def intraday_window(settings: Settings, now: datetime) -> dict[str, Any]:
    """Where the intraday step stands now (R11, R17): the window, whether today is a business day, the next hub
    slot."""
    from .autopilot import INTRADAY_FROM, POLL_LAG_S, POLL_SLOT_S, PRELIM_FROM, Autopilot

    msk = now.astimezone(MSK)
    try:
        business = Autopilot(settings_loader=lambda: settings)._business_day(settings, msk.date())
    except Exception as e:  # noqa: BLE001
        business = f"unknown ({type(e).__name__})"
    t = msk.time()
    window = ("before_intraday" if t < INTRADAY_FROM else "intraday" if t < PRELIM_FROM else "preliminary")
    k = int((now.timestamp() - POLL_LAG_S) // POLL_SLOT_S)
    return {"msk_now": _iso(msk), "today": msk.date().isoformat(), "business_day": business, "window": window,
            "intraday_from": INTRADAY_FROM.isoformat("minutes"), "preliminary_from": PRELIM_FROM.isoformat("minutes"),
            "next_hub_slot": _iso(datetime.fromtimestamp((k + 1) * POLL_SLOT_S + POLL_LAG_S, MSK)),
            "intraday_on": bool(settings.intraday), "hub_configured": bool(settings.hub_url)}


def autopilot_section(settings: Settings, autopilot: Any, scrub: Scrubber, now: datetime,
                      log_cycles: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"in_process": autopilot is not None, "intraday": intraday_window(settings, now)}
    if autopilot is not None:
        st = autopilot.status()
        out["status"] = {"enabled": st.get("enabled"), "state": st.get("state"),
                         "last_cycle_at": st.get("last_cycle_at"), "last_ok_at": st.get("last_ok_at"),
                         "message": scrub.text(st.get("message")),
                         "progress_step": (st.get("progress") or {}).get("step"),
                         "latest_run": _latest_run(st.get("latest_run"))}
        state = autopilot.diagnostics() if hasattr(autopilot, "diagnostics") else {}
        cycles = state.pop("cycles", [])
        out["state"] = state
        out["cycles"] = [_cycle(c, scrub) for c in cycles[-MAX_CYCLES:]][::-1]  # newest first
    out["from_log"] = log_cycles
    return out


def _latest_run(r: dict | None) -> dict | None:
    if not r:
        return None
    return {k: r.get(k) for k in ("run_id", "as_of", "created_at", "run_type", "cut_type", "cut_ts", "label",
                                  "pnl_basis")}


def _cycle(c: dict[str, Any], scrub: Scrubber) -> dict[str, Any]:
    return {"started_at": c.get("started_at"), "duration_s": c.get("duration_s"), "state": c.get("state"),
            "message": scrub.text(c.get("message")),
            "steps": [{"name": s.get("name"), "status": s.get("status"), "duration_s": s.get("duration_s"),
                       "at": s.get("at"), "detail": scrub.text(s.get("detail"))} for s in c.get("steps") or []]}


# ---- logs -------------------------------------------------------------------------------------------------------

RECORD = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)(?:[,.](\d{1,6}))? +(DEBUG|INFO|WARNING|ERROR|CRITICAL) (.*)$")
PREFIXED = re.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL):\s+(.*)$")  # uvicorn's own format
EXC_LINE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Warning|Interrupt|Timeout\w*|Failure))\b(?::.*)?$")
LOGGER = re.compile(r"^[A-Za-z_]\w*(?:\.\w+)+$")
STEP_LINE = re.compile(r"^autopilot: (\w+) (ok|skipped|error): ?(.*)$")
LEVELS = ("WARNING", "ERROR", "CRITICAL")
EMBEDDED_LEVEL = re.compile(r"^\w+: (?:WARN|ERROR)\b")


def _tail(path: Path, max_bytes: int = LOG_TAIL_BYTES) -> tuple[list[str], bool]:
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        data = f.read()
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # a cut first line
    return lines, size > max_bytes


def _records(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """The log records of a file's tail: {at (datetime | None), day, level, logger, message, extra: [lines]}.
    A line that is no record continues the previous one (a traceback); before any record it stands alone."""
    lines, cut = _tail(path)
    stem = path.name.split(".")[0]
    recs: list[dict[str, Any]] = []
    day = None
    for line in lines:
        m = RECORD.match(line)
        if m:
            d, t, frac, level, msg = m.groups()
            at = datetime.fromisoformat(f"{d}T{t}" + (f".{frac.ljust(6, '0')}" if frac else ""))
            day = d
            first = msg.split(" ", 1)[0]
            logger = first if LOGGER.match(first) or first in ("uvicorn", "root") else f"({stem})"
            if logger == first:
                msg = msg[len(first):].lstrip()
            recs.append({"at": at, "day": d, "level": level, "logger": logger, "message": msg, "extra": []})
            continue
        m = PREFIXED.match(line)
        if m:
            recs.append({"at": None, "day": day, "level": m.group(1), "logger": "uvicorn", "message": m.group(2),
                         "extra": []})
            continue
        if recs:
            recs[-1]["extra"].append(line)
        elif re.search(r"(?i)\berror\b|traceback", line):
            recs.append({"at": None, "day": None, "level": "ERROR", "logger": f"({stem})", "message": line,
                         "extra": []})
    return recs, cut


def _exception(rec: dict[str, Any]) -> str | None:
    for line in reversed(rec["extra"]):
        if m := EXC_LINE.match(line.strip()):
            return m.group(1)
    return None


def log_files(settings: Settings) -> list[Path]:
    d = Path(settings.logs_dir)
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir() if p.is_file() and re.fullmatch(r"[\w.-]+\.log(?:\.\d+)?", p.name)]

    def order(p: Path) -> tuple[str, int]:  # rotated first (x.log.3, x.log.2, x.log.1), then x.log
        m = re.fullmatch(r"(.+\.log)(?:\.(\d+))?", p.name)
        return (m.group(1), -int(m.group(2) or 0)) if m else (p.name, 0)

    return sorted(files, key=order)


def logs_section(settings: Settings, scrub: Scrubber, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the ``logs`` section, the autopilot cycles read from ``autopilot.log``)."""
    since = (now - timedelta(days=LOG_DAYS)).date().isoformat()
    files, by_day, by_logger, kinds, recent = [], defaultdict(Counter), defaultdict(Counter), Counter(), []
    kind_last: dict[str, str] = {}
    exceptions: Counter = Counter()
    autopilot_recs: list[dict[str, Any]] = []
    for p in log_files(settings):
        try:
            st = p.stat()
            recs, cut = _records(p)
        except OSError as e:
            files.append({"name": p.name, "error": type(e).__name__})
            continue
        files.append({"name": p.name, "size_kb": round(st.st_size / 1024, 1),
                      "modified": _iso(datetime.fromtimestamp(st.st_mtime, UTC)), "records_read": len(recs),
                      "tail_only": cut})
        if p.name.split(".")[0] == "autopilot":
            autopilot_recs += recs
        for r in recs:
            if r["day"] is not None and r["day"] < since:
                continue
            if r["level"] == "INFO" and EMBEDDED_LEVEL.match(r["message"]):  # "eod: WARN ...": desk eod's own lines
                r["level"] = "WARNING"
            by_day[r["day"] or "unknown"][r["level"]] += 1
            by_logger[r["logger"]][r["level"]] += 1
            if r["level"] in LEVELS:
                exc = _exception(r)
                if exc:
                    exceptions[exc] += 1
                k = f"{r['level']} {r['logger']}: " + signature(scrub, r["message"]) + (f" [{exc}]" if exc else "")
                kinds[k] += 1
                if r["at"]:
                    kind_last[k] = _iso(r["at"])
                recent.append({"at": _iso(r["at"]), "file": p.name, "level": r["level"], "logger": r["logger"],
                               "text": scrub.text(r["message"]), "exception": exc})
    recent.sort(key=lambda r: r["at"] or "")
    out = {"files": files, "days": LOG_DAYS, "times": "the computer's local time (clock.local_utc_offset_h)",
           "by_day": {d: dict(c) for d, c in sorted(by_day.items())},
           "by_logger": {k: dict(c) for k, c in sorted(by_logger.items())},
           "warning_error_kinds": [{"kind": k, "count": n, "last_at": kind_last.get(k)}
                                   for k, n in kinds.most_common(MAX_KINDS)],
           "exceptions": _top(exceptions), "recent_warnings_errors": recent[-MAX_LOG_LINES:][::-1]}
    return out, autopilot_from_log(autopilot_recs, scrub, now)


def autopilot_from_log(recs: list[dict[str, Any]], scrub: Scrubber, now: datetime) -> dict[str, Any]:
    """The autopilot's cycles from its log (``serve`` and ``desk cycle`` both write it): the last ``MAX_CYCLES``
    with each step's status, duration (from the timestamps) and detail; per day of the last ``CYCLE_DAYS`` each
    step's outcomes by kind; the EOD runs the log names with their duration."""
    recs = sorted((r for r in recs if r["at"] is not None), key=lambda r: r["at"])
    cycles: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    mark: datetime | None = None
    eod: list[dict[str, Any]] = []
    since = (now - timedelta(days=CYCLE_DAYS)).date().isoformat()
    per_day: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for r in recs:
        msg = r["message"]
        if msg == "autopilot: cycle start":
            cur, mark = {"started_at": r["at"], "steps": [], "done": None, "message": None}, r["at"]
            cycles.append(cur)
            continue
        if m := STEP_LINE.match(msg):
            name, status, detail = m.groups()
            if cur is None or cur["done"] is not None:
                cur, mark = {"started_at": None, "steps": [], "done": None, "message": None}, None
                cycles.append(cur)
            cur["steps"].append({"name": name, "status": status, "at": _iso(r["at"]),
                                 "duration_s": round((r["at"] - mark).total_seconds(), 1) if mark else None,
                                 "detail": detail})
            mark = r["at"]
            if r["day"] >= since:
                per_day[r["day"]][name][f"{status}: {signature(scrub, detail, 200)}"] += 1
            continue
        if msg.startswith("autopilot: cycle done: ") and cur is not None:
            cur["done"], cur["message"] = r["at"], msg.removeprefix("autopilot: cycle done: ")
            cur = None
            continue
        if m := re.match(r"^eod: desk run (\d+): (\w+), (\d+) warnings", msg):
            eod.append({"at": _iso(r["at"]), "run_id": int(m.group(1)), "status": m.group(2),
                        "warnings": int(m.group(3)), "duration_s": None})
        elif (m := re.match(r"^eod: done in ([\d.]+) s", msg)) and eod and eod[-1]["duration_s"] is None:
            eod[-1]["duration_s"] = float(m.group(1))
    out = []
    for c in cycles[-MAX_CYCLES:][::-1]:
        start = c["started_at"]
        out.append({"started_at": _iso(start), "finished_at": _iso(c["done"]),
                    "duration_s": round((c["done"] - start).total_seconds(), 1) if start and c["done"] else None,
                    "message": scrub.text(c["message"]),
                    "steps": [{**s, "detail": scrub.text(s["detail"])} for s in c["steps"]]})
    return {"times": "the computer's local time (clock.local_utc_offset_h)", "cycles_in_log": len(cycles),
            "last_cycles": out,
            "steps_by_day": {d: {name: _top(c, 15) for name, c in sorted(steps.items())}
                             for d, steps in sorted(per_day.items(), reverse=True)},
            "eod_runs": eod[-15:][::-1]}


# ---- hub and snapshots ------------------------------------------------------------------------------------------


def hub_section(settings: Settings, scrub: Scrubber, live: bool, cached_ids: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"configured": bool(settings.hub_url), "token_set": bool(settings.hub_token),
                           "transport_setting": settings.hub_transport, "checked": False}
    if not (live and settings.hub_url):
        return out
    from .marketdata.hub import HubClient, HubError, hub_check

    st = dataclasses.replace(settings, hub_timeout_s=min(float(settings.hub_timeout_s or HUB_TIMEOUT_S),
                                                         HUB_TIMEOUT_S))
    t0 = time.perf_counter()
    chk = hub_check(st, retries=0)
    out.update(checked=True, reachable=chk.get("health") is not None, ok=chk.get("ok"),
               health=chk.get("health"), token_accepted=chk.get("token_accepted"), transport=chk.get("transport"),
               error=scrub.text(chk.get("error")), check_s=round(time.perf_counter() - t0, 2))
    if api := chk.get("history_api"):  # R24: the history API for this token, or why not
        out["history_api"] = {**api, "reason": scrub.text(api.get("reason"))}
    latest = chk.get("latest") or {}
    out["latest_eod_final"] = {k: latest.get(k) for k in ("business_date", "published_at", "age_hours")} | \
        {"snapshot_id": _id(latest.get("snapshot_id"))} if latest else None
    if not chk.get("token_accepted"):
        return out
    try:
        client = HubClient(st)
        client.retries = 0
        rows = list(islice(client.list_snapshots(kind="market", latest=True), REMOTE_ROWS))
    except (HubError, OSError, ValueError) as e:
        out["remote_error"] = scrub.text(f"{type(e).__name__}: {e}")
        return out
    out["remote"] = [{"business_date": r.get("business_date"), "cut_type": r.get("cut_type"),
                      "cut_ts": r.get("cut_ts"), "published_at": r.get("published_at"),
                      "known_at": r.get("known_at"), "snapshot_id": _id(r.get("snapshot_id")),
                      "static_snapshot_id": _id(r.get("static_snapshot_id")),
                      "cached": r.get("snapshot_id") in cached_ids} for r in rows]
    by: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by[r.get("business_date") or "?"][r.get("cut_type") or "?"] += 1
    out["remote_by_date"] = {d: dict(c) for d, c in sorted(by.items(), reverse=True)}
    return out


_INFO: dict[str, dict[str, Any]] = {}  # snapshot id -> its summary (a bundle never changes)
_RATES: dict[tuple[str, str], dict[str, Any]] = {}  # (market id, static id) -> the rates curve check


def market_info(snap: Any, prov: dict | None) -> dict[str, Any]:
    """A cached market snapshot as codes and counts: envelope, datasets (rows, stale, the hub's coverage), curve
    quote sets per ``source/dataset`` with their quote counts, the expected curves missing, ZCYC and quotes."""
    if snap.id in _INFO:
        return _INFO[snap.id]
    doc, data = snap.doc, snap.doc.get("data") or {}
    datasets = []
    for ds in doc.get("datasets") or []:
        datasets.append({"name": ds.get("name"), "rows": ds.get("rows"), "stale": ds.get("stale"),
                         "stale_reasons": _cap(ds.get("stale_reasons") or []), "coverage": _cap(ds.get("coverage"))})
    sets, by_key = [], Counter()
    for qs in data.get("curve_quote_sets") or []:
        key = f"{qs.get('source')}/{qs.get('dataset')}"
        n = len(qs.get("quotes") or [])
        by_key[key] += n
        sets.append({"key": key, "quote_set_id": qs.get("quote_set_id"), "quote_basis": qs.get("quote_basis"),
                     "snapshot_date": qs.get("snapshot_date"), "fetched_at": qs.get("fetched_at"), "quotes": n,
                     "labels": qs.get("labels"), "market_closed": qs.get("market_closed")})
    expected = (doc.get("parameters") or {}).get("expected_curves") or []
    fixings = Counter(f.get("index_code") for f in data.get("fixings") or []
                      if f.get("value") is not None and f.get("index_code") in RATE_INDICES)
    latest_fix: dict[str, str] = {}
    for f in data.get("fixings") or []:
        code, d = f.get("index_code"), f.get("fixing_date")
        if code in ("RUONIA", "KEY_RATE") and f.get("value") is not None and d and d > latest_fix.get(code, ""):
            latest_fix[code] = d
    excluded = Counter(f"{e.get('dataset')}: {e.get('reason')}" for e in doc.get("excluded") or [])
    info = {
        "snapshot_id": _id(snap.id), "kind": snap.kind, "business_date": snap.business_date, "cut_type": snap.cut_type,
        "cut_ts": doc.get("cut_ts"), "known_at": doc.get("known_at"), "schema_version": doc.get("schema_version"),
        "producer": (doc.get("producer") or {}).get("version"), "supersedes": _id(doc.get("supersedes")),
        "static_snapshot_id": _id(snap.static_snapshot_id),
        "published_at": (prov or {}).get("published_at"), "fetched_at": (prov or {}).get("fetched_at"),
        "from_hub": prov is not None,
        "curve_quotes": dict(sorted(by_key.items())),
        "ruonia_ois_quotes": {k: v for k, v in by_key.items() if k.endswith("/ruonia_ois")},
        "key_rate_swap_quotes": {k: v for k, v in by_key.items() if k.endswith("/key_rate_swap")},
        "expected_curves": expected, "expected_missing": [e for e in expected if not by_key.get(e)],
        "curve_quote_sets": sets,
        "zcyc_present": bool(data.get("zcyc_params")), "bond_quotes": len(data.get("bond_quotes") or []),
        "bond_quotes_intraday": len(data.get("bond_quotes_intraday") or []),
        "fixings": dict(sorted(fixings.items())), "latest_fixing": latest_fix,
        "fx_rates": len(data.get("fx_rates") or []), "cbr_meetings": len(data.get("cbr_meetings") or []),
        "excluded": _top(Counter(excluded), 15), "datasets": datasets,
    }
    _INFO[snap.id] = info
    return info


def rates_check(market: Any, static: Any, scrub: Scrubber) -> dict[str, Any]:
    """Whether the RUONIA OIS curve, the key-rate projection and the fixings build from this market snapshot as a
    run builds them (``orchestrator.with_rates``), with the quote set chosen and the tenors dropped (R14, R15)."""
    key = (market.id, static.id)
    if key in _RATES:
        return _RATES[key]
    from .curves import rates

    def pick(lin: dict[str, Any]) -> dict[str, Any]:
        keep = ("dataset", "source", "quote_basis", "quote_set_id", "fetched_at", "stale_reasons", "last_node",
                "max_repricing_error_bp", "usable", "key_rate_fixing_date", "decision_meeting",
                "next_policy_effective_date", "next_policy_estimated")
        out = {k: lin.get(k) for k in keep if k in lin}
        out["alternatives"] = [a.get("source") for a in lin.get("alternatives") or []]
        out["tenors_used"] = len(lin.get("tenors_used") or [])
        out["tenors_dropped"] = {t: scrub.text(why, 120) for t, why in (lin.get("tenors_dropped") or {}).items()}
        return out

    out: dict[str, Any] = {}
    ois = None
    try:
        ois = rates.ruonia_ois(market, static)
        out["ruonia_ois"] = {"built": True, **pick(ois.lineage)}
    except Exception as e:  # noqa: BLE001 - the reason is the finding
        out["ruonia_ois"] = {"built": False, "error": scrub.text(f"{type(e).__name__}: {e}")}
    if ois is not None:
        try:
            kr = rates.key_rate_projection(market, static, ois.curve)
            out["key_rate"] = {"built": True, **pick(kr.lineage)}
        except Exception as e:  # noqa: BLE001
            out["key_rate"] = {"built": False, "error": scrub.text(f"{type(e).__name__}: {e}")}
    else:
        out["key_rate"] = {"built": False, "error": "not tried: no RUONIA OIS curve to discount on"}
    try:
        _, lin = rates.fixing_entries(market, static)
        out["fixings"] = {code: {k: (len(v) if isinstance(v, list) else v) for k, v in info.items() if k != "calendar"}
                          for code, info in lin.items() if isinstance(info, dict)}
    except Exception as e:  # noqa: BLE001
        out["fixings"] = {"error": scrub.text(f"{type(e).__name__}: {e}")}
    _RATES[key] = out
    return out


def snapshots_section(settings: Settings, scrub: Scrubber) -> dict[str, Any]:
    from .marketdata.bundle import SnapshotDir

    folder = Path(settings.snapshot_cache)
    if not folder.is_dir():
        return {"cache_exists": False, "market": [], "counts": {}}
    sd = SnapshotDir(folder)
    man = sd.manifests()
    counts: Counter = Counter(f"{m.get('kind')}/{m.get('cut_type')}" for m in man.values())
    markets = [(m.get("business_date") or "", m.get("cut_ts") or "", sid, m) for sid, m in man.items()
               if m.get("kind") == "market"]
    markets.sort(reverse=True)
    closes = [x for x in markets if x[3].get("cut_type") != "intraday"]
    dates = sorted({x[0] for x in closes}, reverse=True)[:LOCAL_SNAPSHOT_DATES]
    chosen = [x for x in closes if x[0] in dates] + [x for x in markets if x[3].get("cut_type") == "intraday"][
        :LOCAL_INTRADAY]
    chosen.sort(reverse=True)
    out_rows, checked = [], 0
    by_date: dict[str, Counter] = defaultdict(Counter)
    for d, _, sid, m in markets:
        by_date[d][m.get("cut_type") or "?"] += 1
    for d, _, sid, m in chosen:
        try:
            snap = sd.get(sid)
            info = dict(market_info(snap, sd.provenance(sid)))
            static_cached = bool(snap.static_snapshot_id) and (folder / f"{snap.static_snapshot_id}.json.gz").is_file()
            if not static_cached:
                info["static_cached"] = False
            if checked < RATES_CHECKS:
                if static_cached:
                    info["rates_check"] = rates_check(snap, sd.get(snap.static_snapshot_id), scrub)
                checked += 1
            else:  # in short: the quote counts per source/dataset stay, the sets and the hub's coverage go
                info.pop("curve_quote_sets", None)
                info["datasets"] = {ds["name"]: {"rows": ds["rows"], "stale": ds["stale"]} for ds in info["datasets"]}
            out_rows.append(info)
        except Exception as e:  # noqa: BLE001 - one unreadable bundle is a finding, not the end of the section
            out_rows.append({"snapshot_id": _id(sid), "business_date": d, "cut_type": m.get("cut_type"),
                             "error": scrub.text(f"{type(e).__name__}: {e}")})
    return {"cache_exists": True, "files": len(man), "counts": dict(sorted(counts.items())),
            "market_dates": {d: dict(c) for d, c in sorted(by_date.items(), reverse=True)[:30]},
            "market": out_rows}


def static_section(settings: Settings, scrub: Scrubber, limit: int = 2) -> list[dict[str, Any]]:
    from .marketdata.bundle import SnapshotDir

    folder = Path(settings.snapshot_cache)
    if not folder.is_dir():
        return []
    sd = SnapshotDir(folder)
    statics = sorted(((m.get("business_date") or "", sid) for sid, m in sd.manifests().items()
                      if m.get("kind") == "static"), reverse=True)
    out = []
    for d, sid in statics[:limit]:
        if sid in _INFO:
            out.append(_INFO[sid])
            continue
        try:
            snap = sd.get(sid)
        except Exception as e:  # noqa: BLE001
            out.append({"snapshot_id": _id(sid), "business_date": d, "error": scrub.text(f"{type(e).__name__}: {e}")})
            continue
        doc, data = snap.doc, snap.doc.get("data") or {}
        inst = data.get("instruments") or []
        info = {
            "snapshot_id": _id(sid), "business_date": snap.business_date, "cut_type": snap.cut_type,
            "known_at": doc.get("known_at"), "schema_version": doc.get("schema_version"),
            "producer": (doc.get("producer") or {}).get("version"),
            "instruments": len(inst),
            "by_status": dict(Counter(i.get("status") for i in inst).most_common()),
            "by_bond_class": dict(Counter(i.get("bond_class") for i in inst).most_common()),
            "by_coupon_type": dict(Counter(i.get("coupon_type") for i in inst).most_common()),
            "by_face_unit": dict(Counter(i.get("face_unit") for i in inst).most_common(10)),
            "floater_formulas": dict(Counter(f"{f.get('benchmark')}/{f.get('status')}"
                                             for f in data.get("floater_formulas") or []).most_common()),
            "calendars": [{k: c.get(k) for k in ("calendar", "valid_from", "valid_to", "provisional_from", "version")}
                          for c in data.get("calendars") or []],
            "rows": {k: len(v) for k, v in data.items() if isinstance(v, list)},
            "datasets": [{"name": ds.get("name"), "rows": ds.get("rows"), "stale": ds.get("stale"),
                          "coverage": _cap(ds.get("coverage"))} for ds in doc.get("datasets") or []],
        }
        _INFO[sid] = info
        out.append(info)
    return out


# ---- store ------------------------------------------------------------------------------------------------------


def inbox_section(settings: Settings, con: psycopg.Connection, scrub: Scrubber, now: datetime) -> dict[str, Any]:
    from .inbox import scan

    out: dict[str, Any] = {"folders": {"count": len(settings.inbox_dirs),
                                       "exist": sum(_exists(Path(str(p).removesuffix("**").rstrip("\\/")))
                                                    for p in settings.inbox_dirs),
                                       "recursive": sum(str(p).endswith("**") for p in settings.inbox_dirs)},
                           "lookback_days": settings.inbox_lookback_days}
    try:
        h = scan.adapter(settings)
        out["adapter"] = {"loaded": h.ok, "name": h.name, "version": h.version,
                          "fingerprint": (h.fingerprint or "")[:12] or None,
                          "error": scrub.text(h.error) if not h.ok else None}
    except Exception as e:  # noqa: BLE001
        out["adapter"] = {"loaded": False, "error": scrub.text(f"{type(e).__name__}: {e}")}
    latest = "SELECT DISTINCT ON (sha256) * FROM inbox_file_status ORDER BY sha256, status_id DESC"
    def q(sql: str, *a: Any) -> list[tuple]:
        return con.execute(sql, a).fetchall()

    out["files_by_status"] = {s: n for s, n in q(f"SELECT status, count(*) FROM ({latest}) l GROUP BY 1 ORDER BY 2 DESC")}
    out["files_by_source_status"] = [{"source": src, "status": s, "files": n} for src, s, n in q(
        f"SELECT source, status, count(*) FROM ({latest}) l GROUP BY 1, 2 ORDER BY 1, 2")]
    out["file_errors"] = [{"status": s, "error_code": c, "error_type": t, "files": n} for s, c, t, n in q(
        f"SELECT status, error_code, error_type, count(*) FROM ({latest}) l WHERE error_code IS NOT NULL"
        " OR error_type IS NOT NULL GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 30")]
    out["unread"] = [{"source": src, "status": s, "files": n} for src, s, n in q(
        "SELECT source, status, count(*) FROM inbox_unread GROUP BY 1, 2 ORDER BY 1, 2")]
    out["copies"] = {k: n for k, n in q("SELECT kind, count(*) FROM inbox_sighting GROUP BY 1")}
    out["units_by_status"] = {s: n for s, n in q(
        f"SELECT u.status, count(*) FROM ({latest}) l JOIN inbox_unit u USING (status_id) GROUP BY 1")}
    out["issues"] = [{"origin": o, "severity": s, "code": c, "count": int(n)} for o, s, c, n in q(
        f"SELECT i.origin, i.severity, i.code, sum(i.count) FROM ({latest}) l JOIN inbox_issue i USING (status_id)"
        " GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 40")]
    out["issues_last_7_days"] = [{"severity": s, "code": c, "count": int(n)} for s, c, n in q(
        "SELECT i.severity, i.code, sum(i.count) FROM inbox_issue i JOIN inbox_file_status f USING (status_id)"
        " WHERE f.at >= now() - interval '7 days' GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 40")]
    out["extensions"] = {(e or "(none)"): n for e, n in q(
        "SELECT lower(substring(name from '\\.[^.]*$')), count(*) FROM inbox_file GROUP BY 1 ORDER BY 2 DESC LIMIT 10")}
    out["latest_report_dates"] = [{"source": src, "status": s, "report_date": _iso(rd), "valid_date": _iso(vd)}
                                  for src, s, rd, vd in q(
        f"SELECT DISTINCT ON (source, status) source, status, report_date, valid_date FROM ({latest}) l"
        " WHERE report_date IS NOT NULL ORDER BY source, status, report_date DESC")]
    last = con.execute("SELECT max(at) FROM inbox_file_status").fetchone()[0]
    last_unread = con.execute("SELECT max(at) FROM inbox_unread").fetchone()[0]
    out["last_file_status_at"], out["last_unread_at"] = _iso(last), _iso(last_unread)
    out["events_last_7_days"] = {t: n for t, n in q(
        "SELECT type, count(*) FROM event WHERE at >= now() - interval '7 days' GROUP BY 1 ORDER BY 2 DESC")}
    out["waiting_calendar_report_dates"] = [_iso(d) for (d,) in q(
        f"SELECT DISTINCT report_date FROM ({latest}) l WHERE status = 'waiting_calendar' ORDER BY 1 DESC LIMIT 20")]
    out["reconcile"] = reconcile_summary(scan.reconcile(con, settings))
    return out


def reconcile_summary(r: dict[str, Any]) -> dict[str, Any]:
    """``desk inbox reconcile`` without books and files (R22): the totals of rows (report, accepted, in the run,
    valued, rejected), the books by flag and by valid date age, the loss codes with counts, the file statuses and
    error types, the last scan's counts. Counts and codes only."""
    flags = Counter(f for b in r["books"] for f in b["flags"])
    return {"date": r["date"], "has_default_run": r["run_id"] is not None, "since": r["since"],
            "totals": r["totals"], "books_by_flag": dict(flags.most_common()),
            "books_by_age_days": dict(Counter(str(b["age_days"]) for b in r["books"]
                                              if b["age_days"] is not None).most_common(15)),
            "issue_codes": dict(sum((Counter(b["issues"]) for b in r["books"]), Counter()).most_common(20)),
            "losses": {x["code"]: x["count"] for x in r["losses"]}, "files": r["files"],
            "file_errors": r["file_errors"],
            "last_scan": {k: v for k, v in (r.get("last_scan") or {}).items() if k != "at"} or None}


def positions_section(settings: Settings, con: psycopg.Connection, now: datetime) -> dict[str, Any]:
    from .inbox import scan

    today = now.astimezone(MSK).date()
    sets = [{"valid_date": _iso(d), "sets": n, "books": b, "max_revision": r, "rows": int(rows), "sources": srcs}
            for d, n, b, r, rows, srcs in con.execute(
        "SELECT s.valid_date, count(*), count(DISTINCT s.book_id), max(s.revision), coalesce(sum(n.c), 0),"
        " array_agg(DISTINCT s.source) FROM position_set s"
        " LEFT JOIN (SELECT set_id, count(*) AS c FROM position_input GROUP BY 1) n USING (set_id)"
        " WHERE s.valid_date >= %s GROUP BY 1 ORDER BY 1 DESC", (today - timedelta(days=21),)).fetchall()]
    inputs = store.positions_for(con, today)
    kinds = Counter((inputs.extra.get(pid) or {}).get("asset_class") or ("bond" if secid else "unknown")
                    for pid, _, secid, _ in inputs.rows)
    ages = Counter(str((today - vd).days) for vd in inputs.valid_dates.values())
    books = store.current_books(con, today)
    try:
        u = scan.unmapped(settings, inputs, today)
        unmapped = {"count": u.get("count"), "by_reason": u.get("by_reason"), "error": u.get("error"),
                    "static_snapshot": _id(u.get("static_snapshot"))}
    except Exception as e:  # noqa: BLE001
        unmapped = {"error": type(e).__name__}
    return {"sets_by_valid_date": sets,
            "today": {"date": today.isoformat(), "positions": len(inputs.rows), "books": len(inputs.valid_dates),
                      "without_instrument": sum(1 for r in inputs.rows if r[2] is None),
                      "by_asset_class": dict(kinds.most_common()),
                      "valid_date_age_days": dict(sorted(ages.items(), key=lambda kv: int(kv[0]))),
                      "sources": dict(Counter(s.get("source") for s in inputs.sources.values()).most_common())},
            "books": {"in_force": len(books), "active": sum(1 for b in books if b.get("active", True)),
                      "by_type": dict(Counter(b.get("book_type") for b in books).most_common())},
            "unmapped_today": unmapped}


RUN_SQL = ("SELECT r.run_id, r.run_type, r.business_date, r.prev_date, r.cut_type, r.cut_ts, r.prev_cut_type,"
           " r.status, r.started_at, r.completed_at, r.market_snapshot_id, r.static_snapshot_id,"
           " r.prev_market_snapshot_id, r.core_version, r.backend_version, r.lineage, r.coverage, r.error,"
           " r.supersedes_run_id, r.position_set_ids,"
           f" r.run_id IN ({store.DEFAULT_RUNS}) AS is_default, r.run_id IN ({store.CURRENT_RUNS}) AS is_current"
           " FROM run r ORDER BY r.run_id DESC LIMIT %s")
POS_SQL = ("SELECT quality, split_part(reason, ':', 1), fact->>'instr_type', fact->>'ccy', fact->>'coupon_type',"
           " fact->'coupon' IS NULL OR jsonb_typeof(fact->'coupon') = 'null', fact->'extra'->>'coupon_basis',"
           " fact->>'index_name', fact->>'spread_curve',"
           " ARRAY(SELECT jsonb_object_keys(CASE WHEN jsonb_typeof(fact->'risk') = 'object' THEN fact->'risk'"
           "  ELSE '{}'::jsonb END)),"
           " CASE WHEN jsonb_typeof(fact->'extra'->'assumptions') = 'array' THEN fact->'extra'->'assumptions' END,"
           " no_pnl->>'reason', pnl IS NOT NULL, fact IS NOT NULL, pnl->'extra'->>'method', pnl->'extra'->>'flag',"
           " fact->'extra'->>'valuation', book_id"
           " FROM run_position WHERE run_id = %s")


def _rates_lineage(rates: dict[str, Any], scrub: Scrubber) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ("ruonia_ois", "key_rate"):
        lin = rates.get(name)
        if isinstance(lin, dict):
            out[name] = {k: lin.get(k) for k in ("source", "quote_basis", "quote_set_id", "fetched_at", "last_node",
                                                 "max_repricing_error_bp", "usable") if k in lin}
            out[name]["stale_reasons"] = [scrub.text(r, 160) for r in lin.get("stale_reasons") or []]
            out[name]["tenors_dropped"] = len(lin.get("tenors_dropped") or {})
        else:
            out[name] = None
    for k in ("curve_error", "fixings_error"):
        if rates.get(k):
            out[k] = scrub.text(rates[k])
    fx = rates.get("fixings")
    if isinstance(fx, dict):
        out["fixings"] = {code: {"rows": i.get("rows"), "latest": i.get("latest"),
                                 "missing_business_days": len(i.get("missing_business_days") or []),
                                 "unpublished_at_cut": len(i.get("unpublished_at_cut") or [])}
                          for code, i in fx.items() if isinstance(i, dict)}
    return out


def run_summary(con: psycopg.Connection, r: dict[str, Any], scrub: Scrubber) -> dict[str, Any]:
    lin = r["lineage"] or {}
    basis = lin.get("pnl_basis") or {}
    pos = lin.get("positions") or {}
    started, done = r["started_at"], r["completed_at"]
    out: dict[str, Any] = {
        "run_id": r["run_id"], "as_of": _iso(r["business_date"]), "run_type": r["run_type"], "cut_type": r["cut_type"],
        "cut_ts": _iso(r["cut_ts"]), "label": store.slice_label(r["business_date"], r["cut_type"], r["cut_ts"],
                                                                r["run_type"]),
        "status": r["status"], "error": scrub.text(r["error"]) if r["error"] else None,
        "is_default": r["is_default"], "is_current": r["is_current"], "supersedes_run_id": r["supersedes_run_id"],
        "created_at": _iso(started), "completed_at": _iso(done),
        "duration_s": round((done - started).total_seconds(), 1) if started and done else None,
        "logic_version": lin.get("logic_version"), "backend_version": r["backend_version"],
        "core_version": r["core_version"],
        "market_snapshot_id": _id(r["market_snapshot_id"]), "static_snapshot_id": _id(r["static_snapshot_id"]),
        "static_business_date": lin.get("static_business_date"),
        "point_in_time_warning": bool(lin.get("point_in_time_warning")),
        "market_published_at": lin.get("published_at"), "kbd_check_bp": lin.get("kbd_check_bp"),
        "stale_datasets": {k: [scrub.text(x, 160) for x in (v or [])][:10]
                           for k, v in (lin.get("stale_datasets") or {}).items()},
        "rates": _rates_lineage(lin.get("rates") or {}, scrub),
        "model_spreads": lin.get("model_spreads"),
        "position_sets": len(r["position_set_ids"] or []),
        "positions_carried_books": len(pos.get("carried_from") or {}),
        "position_sources": dict(Counter((s or {}).get("source") for s in (pos.get("sources") or {}).values())),
        # B1: whether the run funded its book and how many problems it had (no amounts: they are the desk's figures)
        "funding": {k: (lin.get("funding") or {}).get(k) for k in ("status", "problems", "rules")}
        if lin.get("funding") else None,
    }
    rows = con.execute(POS_SQL, (r["run_id"],)).fetchall()
    quality, reasons, itype, ccy, disc, risk, no_pnl, methods, flags, assume = (Counter() for _ in range(10))
    valuation_kind = Counter()
    books, with_pnl, valued = set(), 0, 0
    coupon = {"null": 0, "basis": Counter(), "null_with_basis": Counter(), "by_type": defaultdict(Counter),
              "float_without_index": 0}
    floaters: dict[str, Counter] = defaultdict(Counter)
    for (q, reason, it, cy, ct, cnull, cbasis, index, spread_curve, rkeys, assumptions, npr, has_pnl, has_fact,
         method, flag, vkind, book) in rows:
        books.add(book)
        quality[q or "not_valued"] += 1
        if reason:
            reasons[reason] += 1
        if not has_fact:
            continue
        valued += 1
        itype[it] += 1
        ccy[cy] += 1
        valuation_kind[vkind] += 1
        if spread_curve:
            disc[spread_curve] += 1
        for c in rkeys or []:
            risk[c] += 1
        for a in assumptions or []:
            assume[signature(scrub, a, 200)] += 1
        if has_pnl:
            with_pnl += 1
            methods[method] += 1
            if flag:
                flags[signature(scrub, flag, 120)] += 1
        elif npr:
            no_pnl[npr] += 1
        if it == "BOND":
            coupon["basis"][cbasis or "none"] += 1
            coupon["by_type"][ct or "none"]["n"] += 1
            if cnull:
                coupon["null"] += 1
                coupon["null_with_basis"][cbasis or "none"] += 1
                coupon["by_type"][ct or "none"]["coupon_null"] += 1
            if ct == "float":
                f = floaters[index or "no index"]
                f["n"] += 1
                f[f"quality {q}"] += 1
                if reason:
                    f[f"reason {reason}"] += 1
                if cnull:
                    f["coupon_null"] += 1
                for c in rkeys or []:
                    f[f"risk on {c}"] += 1
                if not index:
                    coupon["float_without_index"] += 1
    curves = []
    ref = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'curves'", (r["run_id"],)).fetchone()
    for c in (ref[0] if ref and isinstance(ref[0], list) else []):
        curves.append({"curve_id": c.get("curve_id"), "kind": c.get("kind"), "ccy": c.get("ccy"),
                       "points": len(c.get("points") or []), "built": bool(c.get("points"))})
    levels = {lv: n for lv, n in con.execute("SELECT level, count(*) FROM run_log WHERE run_id = %s GROUP BY 1",
                                             (r["run_id"],))}
    risk_curves = {c: {"positions": n, "has_risk": n > 0} for c, n in sorted(risk.items())}
    for c in curves:
        risk_curves.setdefault(c["curve_id"], {"positions": 0, "has_risk": False})
    out.update({
        "positions": len(rows), "books": len(books), "valued": valued,
        "quality": dict(quality.most_common()), "reasons": _top(reasons, 25),
        "by_instr_type": dict(itype.most_common()), "by_ccy": dict(ccy.most_common()),
        "valuation": dict(valuation_kind.most_common()),
        "assumption_kinds": _top(assume, 25), "discount_curve": dict(disc.most_common()),
        "curves": curves, "risk_by_curve": risk_curves,
        "floaters_by_index": {k: dict(v) for k, v in sorted(floaters.items())},
        "coupon": {"bonds": sum(coupon["basis"].values()), "null": coupon["null"],
                   "basis": dict(coupon["basis"].most_common()), "null_by_basis": dict(coupon["null_with_basis"]),
                   "projected": coupon["basis"].get("projected", 0),
                   "by_type": {k: dict(v) for k, v in sorted(coupon["by_type"].items())},
                   "float_without_index": coupon["float_without_index"]},
        "pnl": {"available": with_pnl > 0, "with_pnl": with_pnl, "without_pnl": valued - with_pnl,
                "reasons": dict(no_pnl.most_common()), "methods": dict(methods.most_common()),
                "flags": _top(flags, 10),
                "run_reason": ("no_prev_market" if not r["prev_market_snapshot_id"]
                               else "no_position_pnl" if valued and not with_pnl else None),
                "prev_date": _iso(r["prev_date"]), "prev_cut_type": r["prev_cut_type"],
                "prev_market_snapshot_id": _id(r["prev_market_snapshot_id"]),
                "basis_label": basis.get("label"), "basis_prev_date": basis.get("prev_date"),
                "basis_expected_date": basis.get("expected_date")},
        "log_levels": levels,
    })
    return out


def runs_section(con: psycopg.Connection, scrub: Scrubber, limit: int = MAX_RUNS) -> dict[str, Any]:
    from psycopg.rows import dict_row

    with con.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(RUN_SQL, (limit,)).fetchall()
    counts = {f"{t}/{c}/{s}": n for t, c, s, n in con.execute(
        "SELECT run_type, cut_type, status, count(*) FROM run GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")}
    by_date = [{"as_of": _iso(d), "runs": n, "types": t} for d, n, t in con.execute(
        "SELECT business_date, count(*), array_agg(DISTINCT run_type || '/' || cut_type) FROM run"
        " GROUP BY 1 ORDER BY 1 DESC LIMIT 20")]
    return {"counts": counts, "by_date": by_date, "last": [run_summary(con, r, scrub) for r in rows]}


def db_section(con: psycopg.Connection, settings: Settings) -> dict[str, Any]:
    migrations = [v for (v,) in con.execute("SELECT version FROM schema_migration ORDER BY version")]
    tables = [t for (t,) in con.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")]
    counts = {}
    for t in tables:
        try:
            counts[t] = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
        except psycopg.Error as e:
            counts[t] = f"error {type(e).__name__}"
    size = con.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    ver = con.execute("SHOW server_version").fetchone()[0]
    return {"store": "external" if settings.store_url else "embedded", "migration_level": migrations[-1] if migrations
            else None, "migrations": migrations, "tables": counts, "size_mb": round(size / 2**20, 1),
            "server_version": ver}


# ---- build ------------------------------------------------------------------------------------------------------


def build(settings: Settings | None = None, *, store_url: str | None = None, autopilot: Any = None,
          hub: bool = True, started_at: datetime | None = None, now: datetime | None = None,
          lan_probe: dict[str, Any] | None = None) -> dict[str, Any]:
    """The diagnostics document (see the module doc). ``settings``: default ``desk.settings.load()``; ``store_url``:
    default ``store.resolve_url``; ``autopilot``: the ``desk serve`` thread when in process; ``hub``: ask the hub
    (a live check and its newest snapshots, at most ``HUB_TIMEOUT_S`` a request); ``started_at``: the server's start
    (uptime); ``lan_probe``: ``desk serve``'s self-check of its addresses (``desk.lan.probe_all``).
    Never raises: a section that fails says so in its ``error``."""
    t_all = time.perf_counter()
    now = now or datetime.now(UTC)
    error = None
    if settings is None:
        try:
            settings = load()
        except SettingsError as e:
            settings, error = Settings(), str(e)
    con = None
    store_error = None
    try:
        con = psycopg.connect(store_url or store.resolve_url(settings), autocommit=True, connect_timeout=5,
                              options="-c default_transaction_read_only=on")
    except Exception as e:  # noqa: BLE001 - no store: the file-based sections still go out
        store_error = f"{type(e).__name__}: {e}"
    try:
        scrub = scrubber(settings, con)
        doc: dict[str, Any] = {
            "schema": SCHEMA, "generated_at": _iso(now),
            "privacy": "aggregates only: codes, dates, counts, snapshot id prefixes; no ISIN/SECID, books, "
                       "portfolios, traders, amounts, prices, PnL values, file paths, user or host names, tokens",
        }
        times: dict[str, float] = {}

        def section(name: str, fn: Callable[[], Any]) -> None:
            t0 = time.perf_counter()
            try:
                doc[name] = fn()
            except Exception as e:  # one section's failure is reported, the others still built
                log.warning("diagnostics: section %s failed", name, exc_info=True)
                doc[name] = {"error": scrub.text(f"{type(e).__name__}: {e}")}
            times[name] = round(time.perf_counter() - t0, 3)

        def need_store(fn: Callable[[psycopg.Connection], Any]) -> Callable[[], Any]:
            def run() -> Any:
                if con is None:
                    return {"error": scrub.text(f"store unavailable: {store_error}")}
                return fn(con)
            return run

        section("app", lambda: app_section(started_at, now))
        section("clock", lambda: clock_section(now))
        section("settings", lambda: settings_section(settings, error, scrub))
        section("lan", lambda: lan_section(settings, lan_probe))
        logs_and_cycles: dict[str, Any] = {}

        def logs() -> dict[str, Any]:
            out, cycles = logs_section(settings, scrub, now)
            logs_and_cycles["cycles"] = cycles
            return out

        section("logs", logs)
        section("autopilot", lambda: autopilot_section(settings, autopilot, scrub, now,
                                                       logs_and_cycles.get("cycles") or {}))
        cached: set[str] = set()
        if Path(settings.snapshot_cache).is_dir():
            cached = {p.name.split(".")[0] for p in Path(settings.snapshot_cache).glob("*.json.gz")}
        section("hub", lambda: hub_section(settings, scrub, hub, cached))
        section("snapshots", lambda: snapshots_section(settings, scrub))
        section("market_history", need_store(lambda c: market_history_section(c, scrub)))
        section("static", lambda: static_section(settings, scrub))
        section("inbox", need_store(lambda c: inbox_section(settings, c, scrub, now)))
        section("positions", need_store(lambda c: positions_section(settings, c, now)))
        section("runs", need_store(lambda c: runs_section(c, scrub)))
        section("db", need_store(lambda c: db_section(c, settings)))
        section("backfill", need_store(lambda c: backfill_section(settings, c)))
        section("backup", lambda: backup.diagnostics(settings))
        doc["build"] = {"duration_s": round(time.perf_counter() - t_all, 2), "sections_s": times,
                        "store_error": scrub.text(store_error) if store_error else None, "hub_asked": bool(hub)}
        order = ("schema", "generated_at", "privacy", "app", "clock", "settings", "lan", "autopilot", "hub",
                 "snapshots", "market_history", "static", "inbox", "positions", "runs", "backfill", "backup", "logs", "db",
                 "build")
        doc = {k: doc[k] for k in order if k in doc}
        return scrub.doc(json.loads(json.dumps(doc, ensure_ascii=False, default=str)))
    finally:
        if con is not None:
            con.close()


def lan_section(settings: Settings, probe: dict[str, Any] | None) -> dict[str, Any]:
    """Network access (``desk lan status``) without addresses or names: where it listens (loopback, every address,
    one address), sign-in, HTTPS, the certificate's name count and how many of this computer's names it lacks, the
    firewall rule, and the start's self-check as statuses (``desk.lan.STATUSES``) and times."""
    from . import lan

    host = settings.bind_host
    tls = bool(settings.tls_cert and settings.tls_key)
    out: dict[str, Any] = {"bind": "loopback" if lan.is_loopback(host) else "every_address" if host in lan.WILDCARD
                           else "one_address", "port": settings.port, "auth": settings.auth, "tls": tls,
                           "allow_plain_http": settings.allow_plain_http, "addresses": len(lan.local_addresses())}
    if tls:
        c = lan.cert_info(settings.tls_cert, lan.local_names()) or {}
        out["cert"] = {"exists": c.get("exists"), "names": len(c.get("sans") or []),
                       "missing": len(c.get("missing") or []), "not_after": c.get("not_after"),
                       "error": c.get("error")}
    if out["bind"] != "loopback":
        out["firewall_rule"] = lan.firewall_rule(settings.port)
    if probe:
        out["probe"] = {"at": probe.get("at"),
                        "loopback": {k: probe["loopback"].get(k) for k in ("status", "ms", "detail")},
                        "lan": [{k: r.get(k) for k in ("status", "ms", "detail")} for r in probe.get("lan", [])]}
    return out


def market_history_section(con: psycopg.Connection, scrub: Scrubber) -> dict[str, Any]:
    """The store's market history from the hub's history API (R24): dates, counts, codes; errors scrubbed."""
    from .marketdata import hubhistory

    st = hubhistory.status(con)
    if st["api"]:
        st["api"]["reason"] = scrub.text(st["api"]["reason"])
    st["errors"] = [scrub.text(e, 200) for e in st["errors"]]
    return st


def backfill_section(settings: Settings, con: psycopg.Connection) -> dict[str, Any]:
    """The history of runs (R23, ``desk.backfill.status``: dates and counts) and the concept pages' read model: run
    summaries, the static snapshots' facts (date, coverage counts), the CBR meetings."""
    from . import backfill

    st = backfill.status(con, settings)
    summ = con.execute("SELECT count(*), (SELECT count(*) FROM run WHERE status = 'complete') FROM run_summary").fetchone()
    stat = [{"business_date": d.isoformat(), "snapshot": sid[:12], "coverage": (data or {}).get("coverage")}
            for sid, d, data in con.execute("SELECT snapshot_id, business_date, data->'calendar' FROM md_static_summary"
                                            " ORDER BY business_date DESC")]
    cbr = con.execute("SELECT count(*), max(business_date) FROM md_cbr_meeting").fetchone()
    return {**st, "run_summaries": {"written": summ[0], "complete_runs": summ[1]}, "static_facts": stat,
            "cbr_meetings": {"rows": cbr[0], "as_of": cbr[1].isoformat() if cbr[1] else None}}


def dumps(doc: dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=1, default=str) + "\n"


def file_name(now: datetime | None = None) -> str:
    """``desk-diagnostics-<UTC timestamp>.json``."""
    return f"desk-diagnostics-{(now or datetime.now(UTC)).astimezone(UTC):%Y%m%dT%H%M%SZ}.json"
