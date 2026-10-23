"""Backend settings: where the hub is, how to authenticate, where local files live.

Layers, the first that sets a key wins: process environment ``DESK_*`` > ``<home>/settings.json``
(written by the settings page, ``/settings.html``) > an env file (``backend/.env`` or the path in
``DESK_ENV_FILE``) > defaults. ``load()`` re-reads ``settings.json`` on every call, so a long-running
server picks up edits without a restart. ``backend/.env.example`` lists every variable.

``<home>`` (``desk_home()``) is the per-user data folder, outside the install folder so that a new
version unpacked anywhere finds the same settings and data: ``DESK_HOME`` (environment or env file),
else ``%LOCALAPPDATA%\\DeskMonitor`` on Windows, else ``<repo>/.devdata`` in a git checkout (the dev
layout), else ``~/.local/share/desk-monitor``. It holds ``settings.json``, ``hub_token``,
``adapter-config/``, ``snapshots/``, ``inbox-archive/``, ``pg/`` (embedded store), ``tdm.sqlite`` and
``logs/``; the dev checkout keeps its old names ``.devdata/inbox`` and ``.devdata/desk_pg``.

* ``DESK_HUB_URL``         hub base URL, set per installation (no default: the address is
                           never kept in the repository); HTTPS only, plain HTTP is
                           accepted for localhost (test stubs)
* ``DESK_HUB_TOKEN``       bearer token of this consumer (scope ``read``: snapshots and the history API of hub
                           0.2.0; a ``snapshots`` token serves the snapshots only), or
  ``DESK_HUB_TOKEN_FILE``  a file holding it; else ``<home>/hub_token`` (written by the settings page).
                           Never commit either; the repository is public
* ``DESK_HUB_CA_BUNDLE``   PEM bundle for a TLS-inspecting proxy (inside the work network)
* ``DESK_HUB_TIMEOUT_S``   per-request timeout (default 60)
* ``DESK_HUB_TRANSPORT``   ``auto`` (default: Python, switching to curl when the proxy wants authentication),
                           ``python`` or ``curl`` (the system curl; on Windows SSPI logs in to the proxy as the
                           current Windows user), see ``desk.marketdata.hub``
* ``DESK_PROXY_USER``      proxy login (``DOMAIN\\user``); its password in ``DESK_PROXY_PASSWORD`` or
                           ``<home>/proxy_password`` (written by the settings page, never returned)
* ``DESK_SNAPSHOT_CACHE``  verified ``<snapshot_id>.json.gz`` bundles (default ``<home>/snapshots``)
* ``DESK_SNAPSHOT_DROP``   folder an operator fills by hand when the hub is unreachable (ADR-001)
* ``DESK_STORE_URL``       desk store, a libpq URI ``postgresql://user:password@host:5432/desk``
                           (slice S5). Unset: an embedded server in ``<home>/pg`` (pgserver)
* ``DESK_DB``              the ``tdm`` SQLite prototype store (default ``<home>/tdm.sqlite``),
                           written by ``desk eod --tdm`` during the transition
* ``DESK_REPORT_DIR``      validation reports (none by default; ``<home>/reports`` is the usual place)
* ``DESK_PRIVATE_ADAPTER`` the private adapter (08 §2): a package directory or a module name; unset: the
                           folder in ``DESK_DEFAULT_PRIVATE_ADAPTER`` (process environment only, set by the
                           Windows bundle's start script) if it exists, else the reference adapter
                           ``desk.inbox.reference``
* ``DESK_PRIVATE_ADAPTER_CONFIG``  the adapter's config folder (``ctx.config_dir``; fingerprinted; default
                           ``<home>/adapter-config``, created on demand)
* ``DESK_PRIVATE_ADAPTER_SHA256``  the expected adapter fingerprint; loading fails on a mismatch
* ``DESK_INBOX_DIRS``      report folders separated by ``;``; one ending in ``\\**`` or ``/**`` is recursive
* ``DESK_INBOX_ARCHIVE``   copies of every matched file under its hash (default ``<home>/inbox-archive``)
* ``DESK_INBOX_POLL_SECONDS``, ``DESK_ADAPTER_MATCH_TIMEOUT_S``, ``DESK_ADAPTER_READ_TIMEOUT_S``,
  ``DESK_INBOX_MAX_FILE_MB``  watcher period (60), adapter timeouts (10 per match batch, 600 per read) and
                           the largest file offered (500)
* ``DESK_ADAPTER_CHECK_TIMEOUT_S``  the limit of one ``reference()`` run when a settings form checks a file with
                           the adapter (60; ``desk.settings_forms``); a save never waits for the check
* ``DESK_AUTOPILOT``, ``DESK_AUTOPILOT_SYNC_LAST``  the ``desk serve`` autopilot (on) and the number of
                           latest hub dates it keeps in sync (5)
* ``DESK_INTRADAY``        the autopilot's intraday step (on): today's book on the hub's intraday cuts every half
                           hour, from 19:45 on the preliminary close (R11)
* ``DESK_PROXY_CURVES_FOR_RISK``  (off) the hub's government par-yield proxies of contract 1.3 (``usd_ust_par``,
                           ``eur_aaa_par``, ``eur_bund_par``: UST par, ECB AAA, Bund, **not OIS**) become the points
                           and the day's move of the run's USD / EUR curves, so the rates PnL and the scenario shifts
                           of USD/EUR risk use them. Off: they are shown on the market pages as proxies only and the
                           USD/EUR curves of the risk stay without points (R25)
* ``DESK_FX_FOREIGN_CURVE`` (``fx_implied``) the curve of an FX forward's foreign leg: ``fx_implied`` (the curve implied
                           from the CETS swap points and FORTS futures, ``<CCY> FX``) or ``xccy`` (CNY: the XCCY curve);
                           the other is the fallback when absent. ``DESK_FX_IMPLIED_FUTURES`` (on): futures extend the
                           implied curves past the last swap. ``DESK_FX_SPOT_SOURCE`` (``market``: the MOEX 19:00 chain
                           of the bonds and the OCP; ``cets_tom``: the CETS TOM close). ``DESK_FX_UNLINKED_TERMS`` (off): a
                           trade_terms trade no positions row names is valued as a row of its own.
                           ``DESK_NDF_FIXING_SOURCES`` (``CBR=CBR,ЦБ=CBR,ЦБ РФ=CBR``): the report's NDF fixing source ->
                           the hub's. ``DESK_FUTURES_BY_TICKER`` (off): a non-bond row naming a FORTS FX future by
                           ticker is valued by core. All provisional (curves.fx.FxOptions; DECISIONS.md open questions)
* ``DESK_INBOX_LOOKBACK_DAYS``  the inbox scan and the autopilot's hub sync take report files (by modification
                           time, then by the report date ``match`` gives) and hub dates at most this many days old
                           (35; 0: no limit); a folder holding years of reports is not read. The backfill
                           (``DESK_BACKFILL_FROM``) reads the older ones once.
* ``DESK_HISTORY_BONDS_FROM``, ``DESK_HISTORY_RATES_FROM``  first date of the market history read from the hub's
                           history API (``YYYY-MM-DD``; defaults 2024-01-03 for bonds, where the hub's bond EOD
                           starts, and 2010-01-01 for the KBD, curves, fixings and FX, bounded by what the hub has);
                           the autopilot's history step (``history_days`` > 0) fetches it a little per cycle
* ``DESK_BACKFILL``, ``DESK_BACKFILL_FROM``, ``DESK_BACKFILL_BUDGET_S``  the autopilot's backfill step (on): the
                           default runs of the past business dates from this date (2026-01-05, the hub's first
                           eod_final close) with positions, at most this many seconds a cycle (600), after every
                           other step (``desk.backfill``, R23; ``desk backfill`` does the same at once)
* ``DESK_UPDATE_DIR``      the Windows bundle: a folder (a network share, say) holding update zips that the
                           settings page offers to install (``desk.update``); none by default
* ``DESK_UPDATE_REPO``, ``DESK_UPDATE_TRANSPORT``, ``DESK_UPDATE_DOWNLOADS_DIR``, ``DESK_UPDATE_AUTO_CHECK``  the
                           updater from GitHub Releases (``desk.updater``): the public repository ``owner/name``
                           (default ``FlyingScotman/trading-desk-monitor``), how the server reaches GitHub (``auto``,
                           ``python``, ``curl``, ``powershell``, or ``browser``: downloads by the browser only), the
                           folder the browser saves downloads to (default: the Windows «Загрузки» folder), and the
                           check for a new version when a page is opened (on; only in the Windows bundle)
* ``DESK_AUTH``, ``DESK_BIND_HOST``, ``DESK_PORT``, ``DESK_TLS_CERT``, ``DESK_TLS_KEY``, ``DESK_ALLOW_PLAIN_HTTP``,
  ``DESK_ADMIN_FROM_NETWORK``, ``DESK_SESSION_IDLE_MINUTES``, ``DESK_SESSION_MAX_HOURS``, ``DESK_LOGIN_MAX_FAILURES``,
  ``DESK_LOGIN_LOCKOUT_MINUTES``  users and network access (``desk.api.auth``, read when ``desk serve`` starts):
                           sign-in (off), the address and port (127.0.0.1:8100; another address than loopback
                           needs sign-in, and HTTPS or ``allow_plain_http``), the certificate, admins from other
                           computers (on), session limits (8 h idle, 24 h) and the lockout (5 wrong passwords, 15 min)

Relative paths resolve against the repository (install) root, those of ``settings.json`` against
``<home>``. The proxy comes from the standard ``HTTPS_PROXY`` / ``NO_PROXY`` variables; ``settings.json``
keys ``https_proxy`` / ``no_proxy`` are exported to them by ``load()`` when the real environment has none.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .inbox.runner import is_path_spec

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
DEV_HOME = REPO_ROOT / ".devdata"
SETTINGS_FILE = "settings.json"
TOKEN_FILE = "hub_token"
PROXY_PASSWORD_FILE = "proxy_password"
HUB_TRANSPORTS = ("auto", "python", "curl")
UPDATE_TRANSPORTS = ("auto", "python", "curl", "powershell", "browser")
DEFAULT_UPDATE_REPO = "FlyingScotman/trading-desk-monitor"
_REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
ADAPTER_CONFIG_DIR = "adapter-config"
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

# settings.json key -> (DESK_* variable, kind); kinds are checked by check_settings_doc
JSON_KEYS: dict[str, tuple[str, str]] = {
    "hub_url": ("DESK_HUB_URL", "str"),
    "hub_ca_bundle": ("DESK_HUB_CA_BUNDLE", "path"),
    "hub_timeout_s": ("DESK_HUB_TIMEOUT_S", "num"),
    "hub_transport": ("DESK_HUB_TRANSPORT", "str"),
    "proxy_user": ("DESK_PROXY_USER", "str"),
    "snapshot_drop": ("DESK_SNAPSHOT_DROP", "path"),
    "store_url": ("DESK_STORE_URL", "str"),
    "private_adapter": ("DESK_PRIVATE_ADAPTER", "adapter"),
    "private_adapter_config": ("DESK_PRIVATE_ADAPTER_CONFIG", "path"),
    "inbox_dirs": ("DESK_INBOX_DIRS", "dirs"),
    "inbox_poll_seconds": ("DESK_INBOX_POLL_SECONDS", "num"),
    "adapter_check_timeout_s": ("DESK_ADAPTER_CHECK_TIMEOUT_S", "num"),
    "autopilot": ("DESK_AUTOPILOT", "bool"),
    "autopilot_sync_last": ("DESK_AUTOPILOT_SYNC_LAST", "int"),
    "intraday": ("DESK_INTRADAY", "bool"),
    "proxy_curves_for_risk": ("DESK_PROXY_CURVES_FOR_RISK", "bool"),
    "fx_foreign_curve": ("DESK_FX_FOREIGN_CURVE", "str"),
    "fx_implied_futures": ("DESK_FX_IMPLIED_FUTURES", "bool"),
    "fx_spot_source": ("DESK_FX_SPOT_SOURCE", "str"),
    "fx_unlinked_terms": ("DESK_FX_UNLINKED_TERMS", "bool"),
    "ndf_fixing_sources": ("DESK_NDF_FIXING_SOURCES", "str"),
    "futures_by_ticker": ("DESK_FUTURES_BY_TICKER", "bool"),
    "inbox_lookback_days": ("DESK_INBOX_LOOKBACK_DAYS", "int0"),
    "update_dir": ("DESK_UPDATE_DIR", "path"),
    "update_repo": ("DESK_UPDATE_REPO", "str"),
    "update_transport": ("DESK_UPDATE_TRANSPORT", "str"),
    "update_downloads_dir": ("DESK_UPDATE_DOWNLOADS_DIR", "path"),
    "update_auto_check": ("DESK_UPDATE_AUTO_CHECK", "bool"),
    "history_days": ("DESK_HISTORY_DAYS", "int0"),
    "history_bonds_from": ("DESK_HISTORY_BONDS_FROM", "date"),
    "history_rates_from": ("DESK_HISTORY_RATES_FROM", "date"),
    "backfill": ("DESK_BACKFILL", "bool"),
    "backfill_from": ("DESK_BACKFILL_FROM", "date"),
    "backfill_budget_s": ("DESK_BACKFILL_BUDGET_S", "num"),
    # users and network access (api/auth.py); read when desk serve starts
    "auth": ("DESK_AUTH", "bool"),
    "bind_host": ("DESK_BIND_HOST", "str"),
    "port": ("DESK_PORT", "int"),
    "tls_cert": ("DESK_TLS_CERT", "path"),
    "tls_key": ("DESK_TLS_KEY", "path"),
    "allow_plain_http": ("DESK_ALLOW_PLAIN_HTTP", "bool"),
    "admin_from_network": ("DESK_ADMIN_FROM_NETWORK", "bool"),
    "session_idle_minutes": ("DESK_SESSION_IDLE_MINUTES", "int"),
    "session_max_hours": ("DESK_SESSION_MAX_HOURS", "int"),
    "login_max_failures": ("DESK_LOGIN_MAX_FAILURES", "int"),
    "login_lockout_minutes": ("DESK_LOGIN_LOCKOUT_MINUTES", "int"),
}
PROXY_KEYS = {"https_proxy": "HTTPS_PROXY", "no_proxy": "NO_PROXY"}
READ_ONLY_KEYS = ("hub_token", "hub_token_set", "proxy_password", "proxy_password_set", "home", "effective",
                  "env_overrides", "error")
_TRUE, _FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off")


class SettingsError(ValueError):
    pass


def in_checkout() -> bool:
    """Running from a git checkout (``.git`` is a folder, or a file in a worktree)."""
    return (REPO_ROOT / ".git").exists()


def desk_home(environ: dict[str, str] | None = None, *, windows: bool | None = None) -> Path:
    """The per-user data folder (see the module doc); not created here. ``windows`` overrides the platform
    check (tests)."""
    env = os.environ if environ is None else environ
    value = env.get("DESK_HOME")
    if not value:
        env_file = _env_file(env)
        value = read_env_file(env_file).get("DESK_HOME") if env_file.is_file() else None
    if value:
        p = Path(value).expanduser()
        return p if p.is_absolute() else REPO_ROOT / p
    if os.name == "nt" if windows is None else windows:
        base = env.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "DeskMonitor"
    if in_checkout():
        return DEV_HOME
    return Path.home() / ".local" / "share" / "desk-monitor"


@dataclass(frozen=True)
class Settings:
    hub_url: str | None = None
    hub_token: str | None = field(default=None, repr=False)
    hub_token_source: str = "not set"
    hub_ca_bundle: Path | None = None
    hub_timeout_s: float = 60.0
    hub_transport: str = "auto"
    proxy_user: str | None = None
    proxy_password: str | None = field(default=None, repr=False)
    snapshot_cache: Path = None  # type: ignore[assignment]  # None: <home>/snapshots
    snapshot_drop: Path | None = None
    store_url: str | None = field(default=None, repr=False)
    db: Path = None  # type: ignore[assignment]  # None: <home>/tdm.sqlite
    report_dir: Path | None = None
    private_adapter: str | None = None
    private_adapter_config: Path | None = None  # load() defaults it to <home>/adapter-config
    private_adapter_sha256: str | None = None
    inbox_dirs: tuple[Path, ...] = ()
    inbox_archive: Path = None  # type: ignore[assignment]  # None: <home>/inbox-archive
    inbox_poll_seconds: float = 60.0
    adapter_match_timeout_s: float = 60.0  # per match call (50 files); the worker start-up is apart (R22)
    adapter_read_timeout_s: float = 600.0
    adapter_check_timeout_s: float = 60.0  # one reference() run of a settings form's check (desk.settings_forms)
    inbox_max_file_mb: float = 500.0
    home: Path = None  # type: ignore[assignment]  # None: desk_home()
    autopilot: bool = True
    autopilot_sync_last: int = 5
    intraday: bool = True  # the autopilot values today's book on the hub's intraday / preliminary cuts (R11)
    proxy_curves_for_risk: bool = False  # UST par / ECB AAA / Bund proxies as the run's USD/EUR curves (R25; not OIS)
    # provisional FX choices (curves.fx.FxOptions; docs/program/DECISIONS.md open questions)
    fx_foreign_curve: str = "fx_implied"  # an FX forward's foreign leg: fx_implied (swap points) or xccy (CNY)
    fx_implied_futures: bool = True  # FORTS FX futures past the last swap point extend the FX-implied curves
    fx_spot_source: str = "market"  # FX forwards' spot: market (MOEX 19:00 chain, as bonds) or cets_tom
    fx_unlinked_terms: bool = False  # value a trade_terms trade no positions row names as a row of its own
    ndf_fixing_sources: str = "CBR=CBR,ЦБ=CBR,ЦБ РФ=CBR"  # the report's NDF fixing source -> the hub's
    futures_by_ticker: bool = False  # a non-bond row naming a FORTS FX future by ticker is valued by core (P3)
    inbox_lookback_days: int = 35  # report dates at most this many days old (R22); 0: no limit
    history_days: int = 260  # hub business dates of market history for the bond charts (~1 year); 0: the cache only
    history_bonds_from: date = date(2024, 1, 3)  # the hub's history API: bonds from (its bond EOD starts here)
    history_rates_from: date = date(2010, 1, 1)  # the hub's history API: KBD, curves, fixings, FX from
    private_adapter_default: bool = False  # private_adapter came from DESK_DEFAULT_PRIVATE_ADAPTER
    update_dir: Path | None = None  # update zips of the Windows bundle (desk.update)
    update_repo: str = DEFAULT_UPDATE_REPO  # GitHub Releases of the monitor (desk.updater)
    update_transport: str = "auto"  # how the server reaches GitHub; "browser": only the browser downloads
    update_downloads_dir: Path | None = None  # the browser's downloads folder; None: the Windows default
    update_auto_check: bool = True  # look for a new release when a page is opened (Windows bundle only)
    backfill: bool = True  # the autopilot computes the default runs of past dates (desk.backfill, R23)
    backfill_from: str = "2026-01-05"  # first date of the backfill (the hub's first eod_final close)
    backfill_budget_s: float = 600.0  # seconds of backfill runs per autopilot cycle (after every other step)
    # users and network access (desk.api.auth; DECISIONS DEC13): read by desk serve when it starts
    auth: bool = False  # sign-in with the monitor's own accounts; off: this computer only
    bind_host: str = "127.0.0.1"  # the address desk serve listens on; another than loopback needs auth
    port: int = 8100
    tls_cert: Path | None = None  # HTTPS: a PEM certificate and its key (desk lan enable --self-signed makes them)
    tls_key: Path | None = None
    allow_plain_http: bool = False  # a network address over plain HTTP (with a warning on the pages)
    admin_from_network: bool = True  # admins may use the settings from other computers (auth on)
    session_idle_minutes: int = 480  # a session ends after this long without a request
    session_max_hours: int = 24  # ... and this long after the sign-in in any case
    login_max_failures: int = 5  # wrong passwords in a row before the account is locked ...
    login_lockout_minutes: int = 15  # ... for this long

    def __post_init__(self) -> None:
        object.__setattr__(self, "home", Path(self.home) if self.home is not None else desk_home())
        dev = self.home == DEV_HOME  # the dev checkout keeps its old folder names
        for name, leaf in (("snapshot_cache", "snapshots"), ("db", "tdm.sqlite"),
                           ("inbox_archive", "inbox" if dev else "inbox-archive")):
            if getattr(self, name) is None:
                object.__setattr__(self, name, self.home / leaf)

    @property
    def settings_file(self) -> Path:
        return self.home / SETTINGS_FILE

    @property
    def token_file(self) -> Path:
        return self.home / TOKEN_FILE

    @property
    def pg_dir(self) -> Path:
        """The embedded store's cluster (no DESK_STORE_URL). The dev checkout keeps ``.devdata/desk_pg``:
        ``.devdata/pg`` is the hub seed cluster there (ADR-006)."""
        return self.home / ("desk_pg" if self.home == DEV_HOME else "pg")

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    def describe(self) -> list[tuple[str, str]]:
        """Effective settings for ``desk config``; the token and the store password are masked."""
        token = (f"{self.hub_token[:4]}…{self.hub_token[-4:]} ({self.hub_token_source})" if self.hub_token
                 else self.hub_token_source)
        return [
            ("home", str(self.home)),
            ("hub_url", self.hub_url or "not set (DESK_HUB_URL)"),
            ("hub_token", token),
            ("hub_ca_bundle", str(self.hub_ca_bundle or "system default")),
            ("hub_timeout_s", f"{self.hub_timeout_s:g}"),
            ("https_proxy", mask_proxy(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "none")),
            ("hub_transport", self.hub_transport),
            ("proxy_user", self.proxy_user or "none"),
            ("proxy_password", "set" if self.proxy_password else "not set"),
            ("snapshot_cache", str(self.snapshot_cache)),
            ("snapshot_drop", str(self.snapshot_drop or "none")),
            ("store_url", mask_url(self.store_url) if self.store_url else f"not set: embedded store ({self.pg_dir})"),
            ("db", str(self.db)),
            ("report_dir", str(self.report_dir or "none")),
            ("private_adapter", (self.private_adapter + (" (bundle default)" if self.private_adapter_default else ""))
             if self.private_adapter else "reference (desk.inbox.reference)"),
            ("private_adapter_config", str(self.private_adapter_config or "none")),
            ("private_adapter_sha256", self.private_adapter_sha256 or "not pinned"),
            ("inbox_dirs", "; ".join(map(str, self.inbox_dirs)) or "none"),
            ("inbox_archive", str(self.inbox_archive)),
            ("inbox_poll_seconds", f"{self.inbox_poll_seconds:g}"),
            ("adapter_match_timeout_s", f"{self.adapter_match_timeout_s:g}"),
            ("adapter_read_timeout_s", f"{self.adapter_read_timeout_s:g}"),
            ("adapter_check_timeout_s", f"{self.adapter_check_timeout_s:g}"),
            ("inbox_max_file_mb", f"{self.inbox_max_file_mb:g}"),
            ("autopilot", "on" if self.autopilot else "off"),
            ("autopilot_sync_last", str(self.autopilot_sync_last)),
            ("intraday", "on" if self.intraday else "off"),
            ("proxy_curves_for_risk", "on (UST par / ECB AAA / Bund, not OIS)" if self.proxy_curves_for_risk
             else "off (proxies shown only)"),
            ("fx_foreign_curve", self.fx_foreign_curve + " (provisional)"),
            ("fx_implied_futures", "on" if self.fx_implied_futures else "off"),
            ("fx_spot_source", self.fx_spot_source),
            ("fx_unlinked_terms", "on" if self.fx_unlinked_terms else "off"),
            ("ndf_fixing_sources", self.ndf_fixing_sources),
            ("futures_by_ticker", "on" if self.futures_by_ticker else "off"),
            ("inbox_lookback_days", str(self.inbox_lookback_days) if self.inbox_lookback_days else "0 (no limit)"),
            ("update_dir", str(self.update_dir or "none")),
            ("update_repo", self.update_repo),
            ("update_transport", self.update_transport),
            ("update_downloads_dir", str(self.update_downloads_dir or "default (the Windows Downloads folder)")),
            ("update_auto_check", "on" if self.update_auto_check else "off"),
            ("history_days", str(self.history_days) if self.history_days else "0 (off: the cached dates only)"),
            ("history_bonds_from", self.history_bonds_from.isoformat()),
            ("history_rates_from", self.history_rates_from.isoformat()),
            ("backfill", (f"on from {self.backfill_from}, {self.backfill_budget_s:g} s a cycle" if self.backfill
                          else "off")),
            ("auth", "on (sign-in with the monitor's accounts)" if self.auth else "off (this computer only)"),
            ("bind_host", f"{self.bind_host}:{self.port}"),
            ("tls", f"{self.tls_cert} / {self.tls_key}" if self.tls_cert else "off (HTTP)"),
            ("allow_plain_http", "on" if self.allow_plain_http else "off"),
            ("admin_from_network", "on" if self.admin_from_network else "off (admin pages from this computer only)"),
            ("sessions", f"idle {self.session_idle_minutes} min, at most {self.session_max_hours} h"),
            ("login_lockout", f"{self.login_max_failures} wrong passwords -> {self.login_lockout_minutes} min"),
        ]


def mask_url(url: str) -> str:
    """The URL without its password, for printing."""
    return re.sub(r"(password=)\S+", r"\1***", re.sub(r"(://[^:/@?]*:)[^@]*@", r"\1***@", url))


def mask_proxy(url: str | None) -> str | None:
    """A proxy address without the password of its ``user:password@`` (with or without a scheme)."""
    return re.sub(r"^((?:[A-Za-z][A-Za-z0-9+.-]*://)?[^:/@]*:)[^@/]*@", r"\1***@", url) if url else url


def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines; blank lines and ``#`` comments skipped (also after an unquoted value),
    surrounding quotes removed."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        out[key.strip()] = value
    return out


def _path(value: str | None, base: Path = REPO_ROOT) -> Path | None:
    if not value:
        return None
    p = Path(value).expanduser()
    return p if p.is_absolute() else base / p


def _env_file(env: Any) -> Path:
    return _path(env.get("DESK_ENV_FILE")) or BACKEND_ROOT / ".env"


# ---- settings.json -------------------------------------------------------------------------------------------


def read_settings_json(home: Path | None = None) -> dict[str, Any]:
    """``<home>/settings.json`` as a dict ({} when missing); SettingsError when it is not a JSON object."""
    path = (home or desk_home()) / SETTINGS_FILE
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise SettingsError(f"{path}: {e}") from None
    try:
        doc = json.loads(text) if text.strip() else {}
    except ValueError as e:
        raise SettingsError(f"{path}: invalid JSON: {e}") from None
    if not isinstance(doc, dict):
        raise SettingsError(f"{path}: must be a JSON object")
    return doc


def check_settings_doc(doc: object) -> list[str]:
    """Shape errors of a settings.json document (unknown keys are kept and ignored; null means unset)."""
    if not isinstance(doc, dict):
        return ["settings must be a JSON object"]
    errors = []
    for key, (_, kind) in {**JSON_KEYS, **{k: (v, "str") for k, v in PROXY_KEYS.items()}}.items():
        v = doc.get(key)
        if v is None:
            continue
        number = isinstance(v, (int, float)) and not isinstance(v, bool)
        if kind in ("str", "path", "adapter") and not isinstance(v, str):
            errors.append(f"{key}: must be a string")
        elif kind == "num" and not (number and v > 0):
            errors.append(f"{key}: must be a positive number")
        elif kind == "int" and not (number and v == int(v) and v >= 1):
            errors.append(f"{key}: must be a whole number >= 1")
        elif kind == "int0" and not (number and v == int(v) and v >= 0):
            errors.append(f"{key}: must be a whole number >= 0")
        elif kind == "bool" and not isinstance(v, bool):
            errors.append(f"{key}: must be true or false")
        elif kind == "date" and not (isinstance(v, str) and (not v.strip() or _is_date(v.strip()))):
            errors.append(f"{key}: must be a date YYYY-MM-DD")
        elif kind == "dirs":
            if not isinstance(v, list):
                errors.append(f"{key}: must be a list of {{path, label, recursive}}")
                continue
            for i, d in enumerate(v):
                if not (isinstance(d, dict) and isinstance(d.get("path"), str) and d["path"].strip()):
                    errors.append(f"{key}[{i}]: path must be a non-empty string")
                elif not isinstance(d.get("label"), (str, type(None))):
                    errors.append(f"{key}[{i}]: label must be a string")
                elif not isinstance(d.get("recursive"), (bool, type(None))):
                    errors.append(f"{key}[{i}]: recursive must be true or false")
    return errors


def _is_date(v: str) -> bool:
    v = v.strip()
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return len(v) == 10


def _json_layer(doc: dict[str, Any], home: Path) -> dict[str, str]:
    """settings.json as ``DESK_*`` strings, relative paths made absolute against ``home`` (inbox_dirs apart)."""
    out = {}
    for key, (var, kind) in JSON_KEYS.items():
        v = doc.get(key)
        if v is None or kind == "dirs" or (isinstance(v, str) and not v.strip()):
            continue
        if kind == "path" or (kind == "adapter" and is_path_spec(v.strip())):
            out[var] = str(_path(v.strip(), home))
        elif kind == "bool":
            out[var] = "1" if v else "0"
        elif kind in ("int", "int0"):
            out[var] = str(int(v))
        else:
            out[var] = v.strip() if isinstance(v, str) else repr(v)
    return out


def _json_dirs(entries: list[dict], home: Path) -> tuple[Path, ...]:
    out = []
    for d in entries:
        p = _path(str(d.get("path") or "").strip(), home)
        if p is not None:
            out.append(p / "**" if d.get("recursive") else p)  # the marker desk.inbox's list_files reads
    return tuple(out)


_exported: dict[str, str] = {}  # proxy variables load() set itself, so it can update or remove them


def _export_proxy(doc: dict[str, Any]) -> None:
    """``https_proxy`` / ``no_proxy`` of settings.json into the environment unless the real environment sets them.
    ``NO_PROXY`` only together with an HTTPS proxy in the environment: on Windows any ``*_PROXY`` variable, even
    ``NO_PROXY`` alone, makes urllib ignore the proxy set in the Windows (Internet Options) settings."""
    for key, var in PROXY_KEYS.items():  # https_proxy first
        want = doc.get(key).strip() if isinstance(doc.get(key), str) else ""
        if var == "NO_PROXY" and not (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")):
            want = ""
        ours = var in _exported and os.environ.get(var) == _exported[var]
        if not ours and (os.environ.get(var) or os.environ.get(var.lower())):
            continue  # the real environment wins
        if want:
            os.environ[var] = _exported[var] = want
        elif ours:
            del os.environ[var]
            del _exported[var]


# ---- load ----------------------------------------------------------------------------------------------------


def load(environ: dict[str, str] | None = None) -> Settings:
    """The effective settings. ``environ`` stands for the process environment (default ``os.environ``); only
    with the real environment are the proxy keys of settings.json exported."""
    proc = dict(os.environ if environ is None else environ)
    home = desk_home(proc)
    env_file = _env_file(proc)
    file_env = read_env_file(env_file) if env_file.is_file() else {}
    doc = read_settings_json(home)
    errors = check_settings_doc(doc)
    if errors:
        raise SettingsError(f"{home / SETTINGS_FILE}: " + "; ".join(errors))
    if environ is None:
        _export_proxy(doc)
    return _build(proc, doc, file_env, home)


def _token(proc: dict[str, str], file_env: dict[str, str], home: Path) -> tuple[str | None, str]:
    """DESK_HUB_TOKEN, then DESK_HUB_TOKEN_FILE of the environment, then <home>/hub_token, then the env file's."""
    for layer in (proc, None, file_env):
        if layer is None:
            f = home / TOKEN_FILE
            if f.is_file() and (t := f.read_text(encoding="utf-8-sig").strip()):
                return t, f"file {f}"
            continue
        if layer.get("DESK_HUB_TOKEN"):
            return layer["DESK_HUB_TOKEN"], "DESK_HUB_TOKEN"
        if layer.get("DESK_HUB_TOKEN_FILE"):
            f = _path(layer["DESK_HUB_TOKEN_FILE"])
            assert f is not None
            if not f.is_file():
                return None, f"not set: {f} does not exist"
            return f.read_text(encoding="utf-8-sig").strip() or None, f"file {f}"
    return None, "not set"


def _build(proc: dict[str, str], doc: dict[str, Any], file_env: dict[str, str], home: Path) -> Settings:
    env: dict[str, str] = {}
    for layer in (file_env, _json_layer(doc, home), proc):  # lowest first
        env.update({k: v for k, v in layer.items() if v})  # empty means unset: it never hides a lower layer
    get = lambda k: env.get(k) or None  # empty means unset

    hub_url = get("DESK_HUB_URL")
    if hub_url is not None:
        hub_url = hub_url.rstrip("/")
        u = urlsplit(hub_url)
        if not (u.scheme == "https" or (u.scheme == "http" and u.hostname in _LOCAL_HOSTS)) or not u.hostname:
            raise SettingsError(f"DESK_HUB_URL must be an https:// URL (http only for localhost): {hub_url!r}")

    token, source = _token(proc, file_env, home)
    transport = (get("DESK_HUB_TRANSPORT") or "auto").strip().lower()
    if transport not in HUB_TRANSPORTS:
        raise SettingsError(f"DESK_HUB_TRANSPORT (hub_transport) must be one of {', '.join(HUB_TRANSPORTS)}, "
                            f"not {transport!r}")
    proxy_password = proc.get("DESK_PROXY_PASSWORD") or file_env.get("DESK_PROXY_PASSWORD") or None
    if proxy_password is None and (pf := home / PROXY_PASSWORD_FILE).is_file():
        proxy_password = pf.read_text(encoding="utf-8-sig").rstrip("\r\n") or None
    defaults = Settings(home=home)

    def num(key: str, default: float) -> float:
        try:
            return float(get(key) or default)
        except ValueError as e:
            raise SettingsError(f"{key}: {e}") from None

    def flag(key: str, default: bool) -> bool:
        v = (get(key) or "").strip().lower()
        if v and v not in _TRUE + _FALSE:
            raise SettingsError(f"{key}: expected 1/0, true/false, yes/no or on/off, not {v!r}")
        return v in _TRUE if v else default

    def choice(key: str, default: str, allowed: tuple[str, ...]) -> str:
        v = (get(key) or default).strip().lower()
        if v not in allowed:
            raise SettingsError(f"{key}: expected one of {', '.join(allowed)}, not {v!r}")
        return v

    def day(key: str, default: date) -> date:
        v = (get(key) or "").strip()
        if v and not _is_date(v):
            raise SettingsError(f"{key}: expected a date YYYY-MM-DD, not {v!r}")
        return date.fromisoformat(v) if v else default

    def count(key: str, default: int, least: int = 1) -> int:
        v = env.get(key)  # "0" is a value here (no limit), not unset
        try:
            n = int(v) if v not in (None, "") else default
        except ValueError as e:
            raise SettingsError(f"{key}: {e}") from None
        if n < least:
            raise SettingsError(f"{key}: must be >= {least}")
        return n

    def dirs(value: str | None) -> tuple[Path, ...]:
        return tuple(p for d in (value or "").split(";") if (p := _path(d.strip())))

    if proc.get("DESK_INBOX_DIRS"):
        inbox_dirs = dirs(proc["DESK_INBOX_DIRS"])
    elif isinstance(doc.get("inbox_dirs"), list):
        inbox_dirs = _json_dirs(doc["inbox_dirs"], home)
    else:
        inbox_dirs = dirs(file_env.get("DESK_INBOX_DIRS"))

    adapter, adapter_default = get("DESK_PRIVATE_ADAPTER"), False
    if adapter is not None and is_path_spec(adapter):
        adapter = str(_path(adapter))
    elif adapter is None and (fallback := _path(proc.get("DESK_DEFAULT_PRIVATE_ADAPTER"))) and fallback.is_dir():
        adapter, adapter_default = str(fallback), True  # the bundle's adapter, below every other layer
    backfill_from = (get("DESK_BACKFILL_FROM") or defaults.backfill_from).strip()
    if not _is_date(backfill_from):
        raise SettingsError(f"DESK_BACKFILL_FROM (backfill_from) must be a date YYYY-MM-DD, not {backfill_from!r}")
    update_repo = (get("DESK_UPDATE_REPO") or defaults.update_repo).strip()
    if not _REPO_RE.fullmatch(update_repo):
        raise SettingsError(f"DESK_UPDATE_REPO (update_repo) must be owner/name of a GitHub repository, "
                            f"not {update_repo!r}")
    update_transport = (get("DESK_UPDATE_TRANSPORT") or "auto").strip().lower()
    if update_transport not in UPDATE_TRANSPORTS:
        raise SettingsError(f"DESK_UPDATE_TRANSPORT (update_transport) must be one of "
                            f"{', '.join(UPDATE_TRANSPORTS)}, not {update_transport!r}")
    bind_host = (get("DESK_BIND_HOST") or defaults.bind_host).strip()
    if not re.fullmatch(r"[A-Za-z0-9.:_-]{1,253}|\[[0-9A-Fa-f:.]+\]", bind_host):
        raise SettingsError(f"DESK_BIND_HOST (bind_host) must be an IP address or a host name, not {bind_host!r}")
    port = count("DESK_PORT", defaults.port)
    if port > 65535:
        raise SettingsError(f"DESK_PORT (port) must be 1..65535, not {port}")
    tls_cert, tls_key = _path(get("DESK_TLS_CERT")), _path(get("DESK_TLS_KEY"))
    if bool(tls_cert) != bool(tls_key):
        raise SettingsError("tls_cert and tls_key (DESK_TLS_CERT, DESK_TLS_KEY) go together: set both or neither")
    store_url = get("DESK_STORE_URL")
    if store_url is not None and urlsplit(store_url).scheme not in ("postgresql", "postgres"):
        raise SettingsError("DESK_STORE_URL must be a postgresql:// URI")
    return Settings(
        hub_url=hub_url,
        hub_token=token,
        hub_token_source=source if token else (source if source.startswith("not set") else "not set"),
        hub_ca_bundle=_path(get("DESK_HUB_CA_BUNDLE")),
        hub_timeout_s=num("DESK_HUB_TIMEOUT_S", defaults.hub_timeout_s),
        hub_transport=transport,
        proxy_user=(get("DESK_PROXY_USER") or "").strip() or None,
        proxy_password=proxy_password,
        snapshot_cache=_path(get("DESK_SNAPSHOT_CACHE")) or defaults.snapshot_cache,
        snapshot_drop=_path(get("DESK_SNAPSHOT_DROP")),
        store_url=store_url,
        db=_path(get("DESK_DB")) or defaults.db,
        report_dir=_path(get("DESK_REPORT_DIR")),
        private_adapter=adapter,
        private_adapter_config=_path(get("DESK_PRIVATE_ADAPTER_CONFIG")) or home / ADAPTER_CONFIG_DIR,
        private_adapter_sha256=get("DESK_PRIVATE_ADAPTER_SHA256"),
        inbox_dirs=inbox_dirs,
        inbox_archive=_path(get("DESK_INBOX_ARCHIVE")) or defaults.inbox_archive,
        inbox_poll_seconds=num("DESK_INBOX_POLL_SECONDS", defaults.inbox_poll_seconds),
        adapter_match_timeout_s=num("DESK_ADAPTER_MATCH_TIMEOUT_S", defaults.adapter_match_timeout_s),
        adapter_read_timeout_s=num("DESK_ADAPTER_READ_TIMEOUT_S", defaults.adapter_read_timeout_s),
        adapter_check_timeout_s=num("DESK_ADAPTER_CHECK_TIMEOUT_S", defaults.adapter_check_timeout_s),
        inbox_max_file_mb=num("DESK_INBOX_MAX_FILE_MB", defaults.inbox_max_file_mb),
        home=home,
        autopilot=flag("DESK_AUTOPILOT", defaults.autopilot),
        autopilot_sync_last=count("DESK_AUTOPILOT_SYNC_LAST", defaults.autopilot_sync_last),
        intraday=flag("DESK_INTRADAY", defaults.intraday),
        proxy_curves_for_risk=flag("DESK_PROXY_CURVES_FOR_RISK", defaults.proxy_curves_for_risk),
        fx_foreign_curve=choice("DESK_FX_FOREIGN_CURVE", defaults.fx_foreign_curve, ("fx_implied", "xccy")),
        fx_implied_futures=flag("DESK_FX_IMPLIED_FUTURES", defaults.fx_implied_futures),
        fx_spot_source=choice("DESK_FX_SPOT_SOURCE", defaults.fx_spot_source, ("market", "cets_tom")),
        fx_unlinked_terms=flag("DESK_FX_UNLINKED_TERMS", defaults.fx_unlinked_terms),
        ndf_fixing_sources=(get("DESK_NDF_FIXING_SOURCES") or defaults.ndf_fixing_sources).strip(),
        futures_by_ticker=flag("DESK_FUTURES_BY_TICKER", defaults.futures_by_ticker),
        inbox_lookback_days=count("DESK_INBOX_LOOKBACK_DAYS", defaults.inbox_lookback_days, 0),
        history_days=count("DESK_HISTORY_DAYS", defaults.history_days, 0),
        history_bonds_from=day("DESK_HISTORY_BONDS_FROM", defaults.history_bonds_from),
        history_rates_from=day("DESK_HISTORY_RATES_FROM", defaults.history_rates_from),
        private_adapter_default=adapter_default,
        update_dir=_path(get("DESK_UPDATE_DIR")),
        update_repo=update_repo,
        update_transport=update_transport,
        update_downloads_dir=_path(get("DESK_UPDATE_DOWNLOADS_DIR")),
        update_auto_check=flag("DESK_UPDATE_AUTO_CHECK", defaults.update_auto_check),
        backfill=flag("DESK_BACKFILL", defaults.backfill),
        backfill_from=backfill_from,
        backfill_budget_s=num("DESK_BACKFILL_BUDGET_S", defaults.backfill_budget_s),
        auth=flag("DESK_AUTH", defaults.auth),
        bind_host=bind_host.strip("[]"),
        port=port,
        tls_cert=tls_cert,
        tls_key=tls_key,
        allow_plain_http=flag("DESK_ALLOW_PLAIN_HTTP", defaults.allow_plain_http),
        admin_from_network=flag("DESK_ADMIN_FROM_NETWORK", defaults.admin_from_network),
        session_idle_minutes=count("DESK_SESSION_IDLE_MINUTES", defaults.session_idle_minutes),
        session_max_hours=count("DESK_SESSION_MAX_HOURS", defaults.session_max_hours),
        login_max_failures=count("DESK_LOGIN_MAX_FAILURES", defaults.login_max_failures),
        login_lockout_minutes=count("DESK_LOGIN_LOCKOUT_MINUTES", defaults.login_lockout_minutes),
    )


def env_overrides(environ: dict[str, str] | None = None) -> list[str]:
    """The settings.json keys the process environment overrides (editing them in settings.json has no effect)."""
    env = os.environ if environ is None else environ
    out = [k for k, (var, _) in JSON_KEYS.items() if env.get(var)]
    return out + [k for k, var in PROXY_KEYS.items() if (env.get(var) or env.get(var.lower()))
                  and not (var in _exported and env.get(var) == _exported[var])]


# ---- writing -------------------------------------------------------------------------------------------------


def atomic_write_text(path: Path, text: str, *, private: bool = False) -> Path:
    """UTF-8 text via a temporary file in the same folder and ``os.replace``: readers never see half a file.
    ``private``: mode 0600 on POSIX (on Windows the per-user profile folder already restricts access)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        if private and os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def update_settings_json(changes: dict[str, Any], home: Path | None = None) -> dict[str, Any]:
    """Merge ``changes`` into ``<home>/settings.json`` (null removes a key, unknown keys are kept), check the
    result and write it atomically; returns the new document. Nothing is written on a SettingsError. The token
    is never stored here (``write_hub_token``). A broken file on disk is kept as ``settings.json.broken``."""
    home = home or desk_home()
    if not isinstance(changes, dict):
        raise SettingsError("settings must be a JSON object")
    path = home / SETTINGS_FILE
    try:
        doc, broken = read_settings_json(home), False
    except SettingsError:
        doc, broken = {}, True
    for k, v in changes.items():
        if k in READ_ONLY_KEYS:
            continue  # read-only or secret: never stored
        if v is None:
            doc.pop(k, None)
        else:
            doc[k] = v
    errors = check_settings_doc(doc)
    if errors:
        raise SettingsError("; ".join(errors))
    _build({}, doc, {}, home)  # the values themselves (URL schemes, numbers)
    if broken:
        os.replace(path, path.with_name(SETTINGS_FILE + ".broken"))
    atomic_write_text(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return doc


def write_hub_token(token: str | None, home: Path | None = None) -> Path | None:
    """Store the hub token in ``<home>/hub_token`` (0600 on POSIX); empty or None removes the file."""
    path = (home or desk_home()) / TOKEN_FILE
    token = (token or "").strip()
    if not token:
        path.unlink(missing_ok=True)
        return None
    if not token.isprintable() or any(c.isspace() for c in token):
        raise SettingsError("the token must be one line without spaces")
    return atomic_write_text(path, token + "\n", private=True)


def write_proxy_password(password: str | None, home: Path | None = None) -> Path | None:
    """Store the proxy password in ``<home>/proxy_password`` (0600 on POSIX); empty or None removes the file."""
    path = (home or desk_home()) / PROXY_PASSWORD_FILE
    if not password:
        path.unlink(missing_ok=True)
        return None
    if "\n" in password or "\r" in password or not password.isprintable():
        raise SettingsError("the proxy password must be one line of printable characters")
    return atomic_write_text(path, password + "\n", private=True)


def adapter_config_dir(settings: Settings, create: bool = True) -> Path | None:
    """The adapter's config folder; the default ``<home>/adapter-config`` is created on demand (a folder set
    explicitly is not: a typo should fail loading, not start an empty config)."""
    cfg = settings.private_adapter_config
    if cfg is not None and create and cfg == settings.home / ADAPTER_CONFIG_DIR:
        cfg.mkdir(parents=True, exist_ok=True)
    return cfg
