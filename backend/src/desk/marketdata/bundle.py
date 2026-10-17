"""Read Market Snapshot Contract v1 bundles and verify their identity.

A bundle file is ``<snapshot_id>.json.gz``: the gzip of the canonical JSON the hub serves from
``/v1/snapshots/{id}/bundle``. ``sha256(gunzip(file)) == snapshot_id`` is checked on every
read. Decimals stay strings; the adapter converts them.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "market_snapshot/1.0"


class SnapshotError(ValueError):
    pass


@dataclass(frozen=True)
class Snapshot:
    id: str
    kind: str
    cut_type: str
    business_date: str
    static_snapshot_id: str | None
    doc: dict[str, Any]

    @property
    def data(self) -> dict[str, list[dict[str, Any]]]:
        return self.doc["data"]


def read_bundle(path: Path) -> Snapshot:
    raw = gzip.decompress(path.read_bytes())
    sid = hashlib.sha256(raw).hexdigest()
    expected = path.name.split(".")[0]
    if sid != expected:
        raise SnapshotError(f"{path.name}: sha256 {sid} does not match the file name")
    doc = json.loads(raw)
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError(f"{path.name}: schema_version {doc.get('schema_version')!r}, need {SCHEMA_VERSION}")
    return Snapshot(sid, doc["kind"], doc["cut_type"], doc["business_date"], doc.get("static_snapshot_id"), doc)


class SnapshotDir:
    """A folder of bundle files (dev export, file drop or a local mirror of the hub)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._manifests: dict[str, dict[str, Any]] = {}
        for p in sorted(self.root.glob("*.json.gz")):
            sid = p.name.split(".")[0]
            with gzip.open(p) as f:
                doc = json.load(f)
            self._manifests[sid] = {k: doc.get(k) for k in ("kind", "cut_type", "business_date", "static_snapshot_id")}

    def get(self, snapshot_id: str) -> Snapshot:
        return read_bundle(self.root / f"{snapshot_id}.json.gz")

    def market_dates(self, cut_type: str = "eod_final") -> list[str]:
        return sorted({m["business_date"] for m in self._manifests.values()
                       if m["kind"] == "market" and m["cut_type"] == cut_type})

    def market(self, business_date: str, cut_type: str = "eod_final") -> Snapshot:
        ids = [sid for sid, m in self._manifests.items()
               if m["kind"] == "market" and m["cut_type"] == cut_type and m["business_date"] == business_date]
        if not ids:
            raise SnapshotError(f"no {cut_type} market snapshot for {business_date} in {self.root}")
        if len(ids) > 1:
            # Corrections supersede; pick the one no other snapshot supersedes.
            superseded = {self.get(i).doc.get("supersedes") for i in ids}
            ids = [i for i in ids if i not in superseded]
        return self.get(ids[0])

    def previous_market_date(self, business_date: str, cut_type: str = "eod_final") -> str | None:
        earlier = [d for d in self.market_dates(cut_type) if d < business_date]
        return earlier[-1] if earlier else None
