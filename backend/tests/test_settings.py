"""desk.settings: defaults, env file, environment precedence, token handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from desk.settings import REPO_ROOT, SettingsError, load


def test_defaults_without_env_file(tmp_path: Path) -> None:
    s = load({"DESK_ENV_FILE": str(tmp_path / "missing.env")})
    assert s.hub_url is None  # the hub address is per installation, never a default
    assert s.hub_token is None and s.hub_token_source == "not set"
    assert s.snapshot_cache == REPO_ROOT / ".devdata" / "snapshots"
    assert not hasattr(s, "db")  # the tdm SQLite store is gone (#294)


def test_env_file_and_environment_precedence(tmp_path: Path) -> None:
    env = tmp_path / "desk.env"
    env.write_text(
        "# comment\nDESK_HUB_URL=https://hub.example/\nDESK_SNAPSHOT_CACHE=cache  # inline\n"
        "DESK_REPORT_DIR='/abs/reports'\nDESK_HUB_TIMEOUT_S=5\n",
        encoding="utf-8",
    )
    s = load({"DESK_ENV_FILE": str(env), "DESK_HUB_TIMEOUT_S": "7"})
    assert s.hub_url == "https://hub.example"
    assert s.snapshot_cache == REPO_ROOT / "cache"
    assert s.report_dir == Path("/abs/reports")
    assert s.hub_timeout_s == 7.0


def test_token_file_and_masking(tmp_path: Path) -> None:
    tok = tmp_path / "hub.token"
    tok.write_text("mdh_abcdefghijklmnop\n", encoding="utf-8")
    s = load({"DESK_ENV_FILE": str(tmp_path / "none"), "DESK_HUB_TOKEN_FILE": str(tok)})
    assert s.hub_token == "mdh_abcdefghijklmnop"
    shown = dict(s.describe())["hub_token"]
    assert "abcdefghijkl" not in shown and shown.startswith("mdh_…mnop")
    assert "mdh_abcdefghijklmnop" not in repr(s)


def test_missing_token_file_is_not_fatal(tmp_path: Path) -> None:
    s = load({"DESK_ENV_FILE": str(tmp_path / "none"), "DESK_HUB_TOKEN_FILE": str(tmp_path / "nope")})
    assert s.hub_token is None and "does not exist" in s.hub_token_source


@pytest.mark.parametrize("url", ["http://hub.example", "ftp://x", "hub.example"])
def test_https_only(tmp_path: Path, url: str) -> None:
    with pytest.raises(SettingsError):
        load({"DESK_ENV_FILE": str(tmp_path / "none"), "DESK_HUB_URL": url})


def test_plain_http_allowed_for_localhost(tmp_path: Path) -> None:
    assert load({"DESK_ENV_FILE": str(tmp_path / "none"), "DESK_HUB_URL": "http://127.0.0.1:9000"}).hub_url \
        == "http://127.0.0.1:9000"


def test_store_url_is_masked_and_checked(tmp_path: Path) -> None:
    url = "postgresql://desk:s3cret@db.internal:5432/desk"
    s = load({"DESK_ENV_FILE": str(tmp_path / "none"), "DESK_STORE_URL": url})
    assert s.store_url == url and "s3cret" not in repr(s)
    assert dict(s.describe())["store_url"] == "postgresql://desk:***@db.internal:5432/desk"
    with pytest.raises(SettingsError):
        load({"DESK_ENV_FILE": str(tmp_path / "none"), "DESK_STORE_URL": "sqlite:///x.db"})


def test_inbox_and_adapter_settings(tmp_path: Path) -> None:
    s = load({"DESK_ENV_FILE": str(tmp_path / "none")})
    assert s.private_adapter is None and s.inbox_dirs == () and s.inbox_archive == REPO_ROOT / ".devdata" / "inbox"
    assert (s.inbox_poll_seconds, s.adapter_match_timeout_s, s.adapter_read_timeout_s, s.inbox_max_file_mb) == \
        (60, 60, 600, 500)
    assert s.adapter_check_timeout_s == 60 and dict(s.describe())["adapter_check_timeout_s"] == "60"
    assert load({"DESK_ENV_FILE": str(tmp_path / "none"), "DESK_ADAPTER_CHECK_TIMEOUT_S": "90"}).adapter_check_timeout_s == 90
    s = load({"DESK_ENV_FILE": str(tmp_path / "none"), "DESK_PRIVATE_ADAPTER": "adapters/tda",
              "DESK_PRIVATE_ADAPTER_CONFIG": "/cfg", "DESK_PRIVATE_ADAPTER_SHA256": "ab" * 32,
              "DESK_INBOX_DIRS": "/in/a; in/b/** ;", "DESK_INBOX_POLL_SECONDS": "5", "DESK_INBOX_MAX_FILE_MB": "1.5"})
    assert s.private_adapter == str(REPO_ROOT / "adapters" / "tda") and s.private_adapter_config == Path("/cfg")
    assert s.inbox_dirs == (Path("/in/a"), REPO_ROOT / "in" / "b" / "**")  # the recursive marker is kept
    assert str(s.inbox_dirs[1]).endswith("**") and s.inbox_poll_seconds == 5 and s.inbox_max_file_mb == 1.5
    assert load({"DESK_ENV_FILE": str(tmp_path / "none"), "DESK_PRIVATE_ADAPTER": "tda"}).private_adapter == "tda"
    shown = dict(s.describe())
    assert shown["inbox_dirs"].count(";") == 1 and shown["private_adapter_sha256"] == "ab" * 32
    assert dict(load({"DESK_ENV_FILE": str(tmp_path / "none")}).describe())["private_adapter"].startswith("reference")
    with pytest.raises(SettingsError, match="DESK_ADAPTER_READ_TIMEOUT_S"):
        load({"DESK_ENV_FILE": str(tmp_path / "none"), "DESK_ADAPTER_READ_TIMEOUT_S": "ten"})
