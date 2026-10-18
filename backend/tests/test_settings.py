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
    assert s.db == REPO_ROOT / ".devdata" / "tdm.sqlite"


def test_env_file_and_environment_precedence(tmp_path: Path) -> None:
    env = tmp_path / "desk.env"
    env.write_text(
        "# comment\nDESK_HUB_URL=https://hub.example/\nDESK_SNAPSHOT_CACHE=cache  # inline\n"
        "DESK_DB='/abs/run.sqlite'\nDESK_HUB_TIMEOUT_S=5\n",
        encoding="utf-8",
    )
    s = load({"DESK_ENV_FILE": str(env), "DESK_HUB_TIMEOUT_S": "7"})
    assert s.hub_url == "https://hub.example"
    assert s.snapshot_cache == REPO_ROOT / "cache"
    assert s.db == Path("/abs/run.sqlite")
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
