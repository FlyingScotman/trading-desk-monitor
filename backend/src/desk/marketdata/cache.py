"""Snapshot cache (slice S3): verified hub bundles in ``DESK_SNAPSHOT_CACHE``.

The cache is a folder of ``<snapshot_id>.json.gz`` files that :class:`~.bundle.SnapshotDir` reads,
so a run never depends on the hub being up. Every file is verified before it lands
(``sha256(gunzip(body)) == snapshot_id``, schema version) and written atomically
(``.part``, then rename). The cache is append-only: the backend never rewrites a bundle, and deletes
only the intraday and preliminary market cuts of past dates (``prune``, R11: one every half hour); a correction is
a new file and ``SnapshotDir.market`` picks the one not superseded.

Beside a bundle pulled from the hub, ``<snapshot_id>.hub.json`` keeps the facts the bundle cannot
carry: the hub host, ``published_at`` and when it was fetched (run lineage, snapshot age).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bundle import SCHEMA_VERSION, SnapshotError
from .hub import HubClient

_ID = re.compile(r"^[0-9a-f]{64}$")


def verify(body: bytes, snapshot_id: str) -> dict[str, Any]:
    """Check a gzipped bundle against its id; return its envelope (everything but ``data``)."""
    try:
        raw = gzip.decompress(body)
    except (OSError, EOFError) as e:
        raise SnapshotError(f"{snapshot_id[:12]}: not a complete gzip body ({e})") from None
    sid = hashlib.sha256(raw).hexdigest()
    if sid != snapshot_id:
        raise SnapshotError(f"{snapshot_id[:12]}: content sha256 is {sid[:12]}, not the snapshot id")
    doc = json.loads(raw)
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError(f"{snapshot_id[:12]}: schema_version {doc.get('schema_version')!r}, need {SCHEMA_VERSION}")
    return {k: v for k, v in doc.items() if k != "data"}


def _write_new(path: Path, data: bytes) -> None:
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    os.replace(part, path)


def bundle_path(cache: Path, snapshot_id: str) -> Path:
    return cache / f"{snapshot_id}.json.gz"


def fetch(client: HubClient, cache: Path, snapshot_id: str, row: dict[str, Any] | None = None) -> bool:
    """Download one bundle unless cached. ``row`` is the hub's list row (for the provenance file).
    Returns True when a new file was written."""
    cache.mkdir(parents=True, exist_ok=True)
    path = bundle_path(cache, snapshot_id)
    new = not path.exists()
    if new:
        body = client.bundle(snapshot_id)
        verify(body, snapshot_id)
        _write_new(path, body)
    side = cache / f"{snapshot_id}.hub.json"
    if not side.exists():
        meta = row or {"snapshot_id": snapshot_id, **client.manifest(snapshot_id)["meta"]}
        prov = {"hub": client.host, "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                **{k: meta.get(k) for k in ("snapshot_id", "kind", "cut_type", "business_date", "published_at",
                                            "superseded_by")}}
        _write_new(side, json.dumps(prov, indent=1).encode())
    return new


@dataclass
class Synced:
    business_date: str
    market_id: str | None = None
    static_id: str | None = None
    published_at: str | None = None
    downloaded: list[str] = field(default_factory=list)


def fetch_row(client: HubClient, cache: Path, row: dict[str, Any]) -> Synced:
    """The market snapshot of a hub list row and the static snapshot it names, unless cached."""
    s = Synced(row["business_date"], row["snapshot_id"], row.get("static_snapshot_id"), row.get("published_at"))
    if fetch(client, cache, row["snapshot_id"], row):
        s.downloaded.append(row["snapshot_id"])
    if s.static_id and fetch(client, cache, s.static_id):
        s.downloaded.append(s.static_id)
    return s


def sync(client: HubClient, cache: Path, dates: Iterable[str], cut_type: str = "eod_final") -> list[Synced]:
    """For each date: the newest non-superseded market snapshot and the static snapshot it names."""
    out = []
    for d in dates:
        rows = list(client.list_snapshots(kind="market", cut_type=cut_type, business_date=d, latest=True))
        out.append(fetch_row(client, cache, rows[0]) if rows else Synced(d))
    return out


PRUNED_CUTS = ("intraday", "eod_preliminary")


def prune(cache: Path, before: str, keep: Iterable[str] = ()) -> list[str]:
    """Delete the intraday and preliminary market bundles (and their ``.hub.json``) of business dates before
    ``before``, except the ids in ``keep`` (the snapshots of the runs still shown). ``eod_final`` and static bundles
    stay. Returns the ids deleted; a file that cannot be deleted now (a lock on Windows) is left for next time."""
    from .bundle import SnapshotDir

    folder, kept, out = Path(cache), set(keep), []
    if not folder.is_dir():
        return out
    sd = SnapshotDir(folder)
    for sid in sd.ids():
        m = sd.manifest(sid) or {}
        if (m.get("kind") != "market" or m.get("cut_type") not in PRUNED_CUTS or sid in kept
                or not m.get("business_date") or m["business_date"] >= before):
            continue
        try:
            bundle_path(folder, sid).unlink(missing_ok=True)
            (folder / f"{sid}.hub.json").unlink(missing_ok=True)
        except OSError:
            continue
        out.append(sid)
    return out


def hub_dates(client: HubClient, cut_type: str = "eod_final") -> Iterator[str]:
    """Business dates with a published market snapshot of ``cut_type``, newest first."""
    seen: set[str] = set()
    for row in client.list_snapshots(kind="market", cut_type=cut_type):
        if row["business_date"] not in seen:
            seen.add(row["business_date"])
            yield row["business_date"]


def import_drop(drop: Path, cache: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """Copy hand-delivered bundles from the drop folder with the same verification (ADR-001 §4).
    Returns (imported ids, [(file name, reason)] rejected); bad files are not copied."""
    cache.mkdir(parents=True, exist_ok=True)
    imported, rejected = [], []
    for p in sorted(Path(drop).glob("*.json.gz")):
        sid = p.name.removesuffix(".json.gz")
        if not _ID.match(sid):
            rejected.append((p.name, "file name is not <snapshot_id>.json.gz"))
            continue
        if bundle_path(cache, sid).exists():
            continue
        body = p.read_bytes()
        try:
            verify(body, sid)
        except (SnapshotError, ValueError) as e:
            rejected.append((p.name, str(e)))
            continue
        _write_new(bundle_path(cache, sid), body)
        imported.append(sid)
    return imported, rejected
