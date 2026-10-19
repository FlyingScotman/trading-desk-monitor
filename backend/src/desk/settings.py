"""Backend settings: where the hub is, how to authenticate, where local files live.

Read from ``DESK_*`` environment variables, then from an env file (``backend/.env`` or the
path in ``DESK_ENV_FILE``); the environment wins. ``backend/.env.example`` lists every key.

* ``DESK_HUB_URL``         hub base URL, set per installation (no default: the address is
                           never kept in the repository); HTTPS only, plain HTTP is
                           accepted for localhost (test stubs)
* ``DESK_HUB_TOKEN``       bearer token of this consumer (scope ``snapshots``), or
  ``DESK_HUB_TOKEN_FILE``  a file holding it. Never commit either; the repository is public
* ``DESK_HUB_CA_BUNDLE``   PEM bundle for a TLS-inspecting proxy (inside the work network)
* ``DESK_HUB_TIMEOUT_S``   per-request timeout (default 60)
* ``DESK_SNAPSHOT_CACHE``  verified ``<snapshot_id>.json.gz`` bundles (default ``.devdata/snapshots``)
* ``DESK_SNAPSHOT_DROP``   folder an operator fills by hand when the hub is unreachable (ADR-001)
* ``DESK_STORE_URL``       desk store, a libpq URI ``postgresql://user:password@host:5432/desk``
                           (slice S5). Unset: an embedded dev server in ``.devdata/desk_pg`` (pgserver)
* ``DESK_DB``              the ``tdm`` SQLite prototype store (default ``.devdata/tdm.sqlite``),
                           written by ``desk eod --tdm`` during the transition
* ``DESK_REPORT_DIR``      validation reports (none by default)

The proxy itself comes from the standard ``HTTPS_PROXY`` / ``NO_PROXY`` variables.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    hub_url: str | None = None
    hub_token: str | None = field(default=None, repr=False)
    hub_token_source: str = "not set"
    hub_ca_bundle: Path | None = None
    hub_timeout_s: float = 60.0
    snapshot_cache: Path = REPO_ROOT / ".devdata" / "snapshots"
    snapshot_drop: Path | None = None
    store_url: str | None = field(default=None, repr=False)
    db: Path = REPO_ROOT / ".devdata" / "tdm.sqlite"
    report_dir: Path | None = None

    def describe(self) -> list[tuple[str, str]]:
        """Effective settings for ``desk config``; the token and the store password are masked."""
        token = (f"{self.hub_token[:4]}…{self.hub_token[-4:]} ({self.hub_token_source})" if self.hub_token
                 else self.hub_token_source)
        return [
            ("hub_url", self.hub_url or "not set (DESK_HUB_URL)"),
            ("hub_token", token),
            ("hub_ca_bundle", str(self.hub_ca_bundle or "system default")),
            ("hub_timeout_s", f"{self.hub_timeout_s:g}"),
            ("https_proxy", os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "none"),
            ("snapshot_cache", str(self.snapshot_cache)),
            ("snapshot_drop", str(self.snapshot_drop or "none")),
            ("store_url", mask_url(self.store_url) if self.store_url else "not set: embedded dev store (.devdata/desk_pg)"),
            ("db", str(self.db)),
            ("report_dir", str(self.report_dir or "none")),
        ]


def mask_url(url: str) -> str:
    """The URL without its password, for printing."""
    return re.sub(r"(password=)\S+", r"\1***", re.sub(r"(://[^:/@?]*:)[^@]*@", r"\1***@", url))


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


def _path(value: str | None) -> Path | None:
    if not value:
        return None
    p = Path(value).expanduser()
    return p if p.is_absolute() else REPO_ROOT / p


def load(environ: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if environ is None else environ)
    env_file = _path(env.get("DESK_ENV_FILE")) or BACKEND_ROOT / ".env"
    if env_file.is_file():
        env = {**read_env_file(env_file), **env}
    get = lambda k: env.get(k) or None  # empty means unset

    hub_url = get("DESK_HUB_URL")
    if hub_url is not None:
        hub_url = hub_url.rstrip("/")
        u = urlsplit(hub_url)
        if not (u.scheme == "https" or (u.scheme == "http" and u.hostname in _LOCAL_HOSTS)) or not u.hostname:
            raise SettingsError(f"DESK_HUB_URL must be an https:// URL (http only for localhost): {hub_url!r}")

    token, source = get("DESK_HUB_TOKEN"), "DESK_HUB_TOKEN"
    if token is None and get("DESK_HUB_TOKEN_FILE"):
        token_file = _path(get("DESK_HUB_TOKEN_FILE"))
        assert token_file is not None
        if token_file.is_file():
            token, source = token_file.read_text(encoding="utf-8").strip() or None, f"file {token_file}"
        else:
            source = f"not set: {token_file} does not exist"

    defaults = Settings()
    try:
        timeout = float(get("DESK_HUB_TIMEOUT_S") or defaults.hub_timeout_s)
    except ValueError as e:
        raise SettingsError(f"DESK_HUB_TIMEOUT_S: {e}") from None
    store_url = get("DESK_STORE_URL")
    if store_url is not None and urlsplit(store_url).scheme not in ("postgresql", "postgres"):
        raise SettingsError("DESK_STORE_URL must be a postgresql:// URI")
    return Settings(
        hub_url=hub_url,
        hub_token=token,
        hub_token_source=source if token else (source if source.startswith("not set") else "not set"),
        hub_ca_bundle=_path(get("DESK_HUB_CA_BUNDLE")),
        hub_timeout_s=timeout,
        snapshot_cache=_path(get("DESK_SNAPSHOT_CACHE")) or defaults.snapshot_cache,
        snapshot_drop=_path(get("DESK_SNAPSHOT_DROP")),
        store_url=store_url,
        db=_path(get("DESK_DB")) or defaults.db,
        report_dir=_path(get("DESK_REPORT_DIR")),
    )
