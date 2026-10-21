"""The snapshot cache's manifest index (``bundle.MANIFEST_INDEX``): listing a folder parses each bundle once, ever, not
once per process (E2E 21.10.2026: 190 bundles of the whole universe took 54 s at the start of every command). A file
added, changed or removed is seen; a broken index or a folder that cannot be written changes nothing."""

from __future__ import annotations

import shutil

import pytest
from test_market_history import FIX, shifted, write

from desk.marketdata import bundle
from desk.marketdata.bundle import MANIFEST_INDEX, SnapshotDir


@pytest.fixture
def folder(tmp_path):
    for p in FIX.glob("*.json.gz"):
        shutil.copy(p, tmp_path)
    return tmp_path


def parses(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = bundle._manifest
    monkeypatch.setattr(bundle, "_MANIFESTS", {})  # a new process: nothing in memory
    monkeypatch.setattr(bundle, "_manifest", lambda p: seen.append(p.name) or real(p))
    return seen


def test_index_lists_without_parsing_again(folder, monkeypatch):
    seen = parses(monkeypatch)
    first = SnapshotDir(folder).manifests()
    assert len(seen) == 2 and (folder / MANIFEST_INDEX).is_file()
    seen = parses(monkeypatch)
    assert SnapshotDir(folder).manifests() == first and seen == []  # read from the index
    sid = write(folder, shifted("2026-10-14", 1))  # a new bundle: only it is parsed
    seen = parses(monkeypatch)
    d = SnapshotDir(folder)
    assert seen == [f"{sid}.json.gz"] and d.market_dates() == ["2026-10-14", "2026-10-15"]
    (folder / f"{sid}.json.gz").unlink()  # pruned: gone from the listing and the index
    seen = parses(monkeypatch)
    assert SnapshotDir(folder).manifests() == first and seen == []
    assert f"{sid}.json.gz" not in (folder / MANIFEST_INDEX).read_text(encoding="utf-8")


def test_broken_index_and_read_only_folder(folder, monkeypatch):
    (folder / MANIFEST_INDEX).write_text("{not json", encoding="utf-8")
    seen = parses(monkeypatch)
    first = SnapshotDir(folder).manifests()
    assert len(seen) == 2 and len(first) == 2  # parsed again, index rewritten
    monkeypatch.setattr(bundle, "_write_index", lambda root, idx: (_ for _ in ()).throw(AssertionError("no write")))
    seen = parses(monkeypatch)
    assert SnapshotDir(folder).manifests() == first and seen == []  # nothing changed: nothing written

    def denied(*a, **kw):
        raise PermissionError("read-only drop folder")

    monkeypatch.undo()
    (folder / MANIFEST_INDEX).unlink()
    monkeypatch.setattr(bundle.os, "replace", denied)
    assert SnapshotDir(folder).manifests() == first and not (folder / MANIFEST_INDEX).exists()
    assert not list(folder.glob(f"{MANIFEST_INDEX}.*.tmp"))  # the temporary file removed
