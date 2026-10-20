"""Read Market Snapshot Contract v1 bundles and verify their identity.

A bundle file is ``<snapshot_id>.json.gz``: the gzip of the canonical JSON the hub serves from
``/v1/snapshots/{id}/bundle``. ``sha256(gunzip(file)) == snapshot_id`` is checked on every
read. Decimals stay strings; the adapter converts them.

Versions (D10): any ``market_snapshot/1.<minor>`` is read. A minor version only adds fields and datasets (the hub's
``schemas/market_snapshot/v1/README.md``), and the adapter reads the fields it knows by name, so unknown ones are
ignored. Another major version (``market_snapshot/2.0``) is refused: its meaning may have changed.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "market_snapshot/1.0"
"""The version the desk was written against; ``supported_schema`` accepts every minor of its major."""
SCHEMA_MAJOR = "market_snapshot/1"
_SCHEMA = re.compile(r"^market_snapshot/1\.(0|[1-9][0-9]*)$")


def supported_schema(version: object) -> bool:
    """True for ``market_snapshot/1.<minor>`` (additive minors), False for anything else (another major, garbage)."""
    return isinstance(version, str) and _SCHEMA.match(version) is not None


def check_schema(doc: dict[str, Any], name: str) -> None:
    if not supported_schema(doc.get("schema_version")):
        raise SnapshotError(f"{name}: schema_version {doc.get('schema_version')!r}, need {SCHEMA_MAJOR}.x")


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
    return parse_bundle(path.read_bytes(), path.name.split(".")[0], path.name)


def parse_bundle(body: bytes, snapshot_id: str, name: str | None = None) -> Snapshot:
    """A bundle's gzipped bytes (a file, or a hub body that is not cached), verified against ``snapshot_id``."""
    name = name or snapshot_id[:12]
    try:
        raw = gzip.decompress(body)
    except (OSError, EOFError) as e:
        raise SnapshotError(f"{name}: not a complete gzip body ({e})") from None
    sid = hashlib.sha256(raw).hexdigest()
    if sid != snapshot_id:
        raise SnapshotError(f"{name}: sha256 {sid} does not match the file name")
    doc = json.loads(raw)
    check_schema(doc, name)
    return Snapshot(sid, doc["kind"], doc["cut_type"], doc["business_date"], doc.get("static_snapshot_id"), doc)


_MANIFESTS: dict[tuple[str, int, int], dict[str, Any]] = {}


def _manifest(path: Path) -> dict[str, Any]:
    """The envelope fields ``SnapshotDir`` lists. A bundle is content-addressed and the cache append-only, so it
    is parsed once per process (an autopilot cycle builds a ``SnapshotDir`` every minute)."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _MANIFESTS:
        with gzip.open(path) as f:
            doc = json.load(f)
        _MANIFESTS[key] = {k: doc.get(k) for k in ("kind", "cut_type", "business_date", "static_snapshot_id",
                                                   "supersedes")}
    return _MANIFESTS[key]


class SnapshotDir:
    """A folder of bundle files (dev export, file drop or a local mirror of the hub)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._manifests: dict[str, dict[str, Any]] = {}
        for p in sorted(self.root.glob("*.json.gz")):
            self._manifests[p.name.split(".")[0]] = _manifest(p)

    def manifests(self) -> dict[str, dict[str, Any]]:
        """snapshot id -> kind, cut_type, business_date, static_snapshot_id of every bundle in the folder."""
        return {sid: dict(m) for sid, m in self._manifests.items()}

    def get(self, snapshot_id: str) -> Snapshot:
        return read_bundle(self.root / f"{snapshot_id}.json.gz")

    def provenance(self, snapshot_id: str) -> dict[str, Any] | None:
        """Hub facts written by ``desk sync`` beside the bundle (host, published_at); None for files
        from a dev export or a drop."""
        p = self.root / f"{snapshot_id}.hub.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

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
            superseded = {self._manifests[i]["supersedes"] for i in ids}
            ids = [i for i in ids if i not in superseded]
        return self.get(ids[0])

    def previous_market_date(self, business_date: str, cut_type: str = "eod_final") -> str | None:
        earlier = [d for d in self.market_dates(cut_type) if d < business_date]
        return earlier[-1] if earlier else None
