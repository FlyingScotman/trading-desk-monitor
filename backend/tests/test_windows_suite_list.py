"""The list of backend tests that CI runs on Windows (``tests/windows_suite.txt``, TDM#639): every line is a test file that
exists, once, so a renamed or deleted file does not turn the Windows job into a run of nothing."""

from __future__ import annotations

from pathlib import Path

LIST = Path(__file__).with_name("windows_suite.txt")


def _files() -> list[str]:
    return [ln.strip() for ln in LIST.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_every_listed_file_exists_once() -> None:
    files = _files()
    assert files, "the Windows job would run nothing"
    assert len(files) == len(set(files)), "a file twice"
    missing = [f for f in files if not (LIST.parents[1] / f).is_file()]
    assert missing == []
    assert all(f.startswith("tests/test_") and f.endswith(".py") for f in files)


def test_the_launchers_and_the_update_are_on_the_list() -> None:
    files = set(_files())
    assert {"tests/test_windows_bundle.py", "tests/test_deskctl_mirror.py", "tests/test_update.py",
            "tests/test_embedded_store_518.py", "tests/test_no_named_timezone.py"} <= files
