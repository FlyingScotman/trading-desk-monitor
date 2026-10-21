"""Reference facts of issuers and bonds from the newest static snapshot of the cache (R25): the issuer's short name,
group, sector, state ownership and the ratings (``ratings.RatingBook``) do not describe a market date, they are what
the hub knows now, so a run (also of a past date) and the pages take them from the newest static the desk has, not
from the static its market snapshot is consistent with (the morning's ``eod_final`` names the static of 08:00; the
hub's 1.2 static with ratings came at 17:30). The terms a valuation needs (coupons, faces, offers) stay the run's own
static's (``EodContext.static``).

``newest_static_id(sd)``: the newest static bundle of a snapshot folder by (business date, known_at, cut time).
``facts(sd, fallback)``: the ``Reference`` of it (parsed once per process and snapshot), the run's own static's
(``fallback``) when the folder has no newer one or it cannot be read.

A sector the hub leaves empty (``sector`` null: the hub's OKVED source not loaded, 1.2 of 21.10.2026 has none) is
given by the bond class for the issuers whose class settles it (``CLASS_SECTOR``: OFZ and sovereign bonds -> the
sovereign, subfederal, municipal), marked ``sector_source = bond_class``; any other stays empty (never guessed).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .adapter import Issuer, StaticView
from .bundle import Snapshot, SnapshotDir, SnapshotError, _utc
from .ratings import RatingBook

log = logging.getLogger(__name__)

CLASS_SECTOR = {"ofz_pd": "sovereign", "ofz_ad": "sovereign", "ofz_pk": "sovereign", "ofz_in": "sovereign",
                "ofz_n": "sovereign", "sovereign_fx": "sovereign", "sovereign_other": "sovereign",
                "subfederal": "subfederal", "municipal": "municipal"}
"""Bond classes that settle the issuer's sector (the hub's own ``rule`` for these classes, ``sector_map.json``)."""


@dataclass
class Reference:
    snapshot_id: str
    business_date: str
    schema_version: str | None
    issuers: dict[int, Issuer]
    ratings: RatingBook
    sector_source: dict[int, str] = field(default_factory=dict)
    """issuer id -> where its sector came from: the hub's ``sector_source`` or ``bond_class``."""

    def issuer(self, issuer_id: int | None) -> Issuer | None:
        return self.issuers.get(int(issuer_id)) if issuer_id is not None else None

    def coverage(self) -> dict[str, Any]:
        n = len(self.issuers)
        return {"snapshot_id": self.snapshot_id, "business_date": self.business_date,
                "schema_version": self.schema_version, "issuers": n,
                "with_short_name": sum(bool(i.short_name) for i in self.issuers.values()),
                "with_sector": sum(bool(i.sector) for i in self.issuers.values()),
                "sector_by_bond_class": sum(v == "bond_class" for v in self.sector_source.values()),
                "with_group": sum(bool(i.group_name) for i in self.issuers.values()),
                "rating_rows": self.ratings.rows}


def build(snap: Snapshot) -> Reference:
    """The reference facts of one static snapshot (module doc)."""
    d = snap.data
    classes: dict[int, Counter[str]] = {}
    bond_issuer: dict[str, int | None] = {}
    for i in d.get("instruments") or ():
        iid = i.get("issuer_id")
        bond_issuer[i["secid"]] = iid
        if iid is not None:
            classes.setdefault(int(iid), Counter())[i.get("bond_class") or "unknown"] += 1
    issuers, source = {}, {}
    for x in d.get("issuers") or ():
        iid = int(x["issuer_id"])
        sector = x.get("sector")
        if sector:
            source[iid] = x.get("sector_source") or "hub"
        else:
            cls = classes.get(iid)
            settled = {CLASS_SECTOR.get(c) for c in cls} if cls else {None}
            if len(settled) == 1 and (s := next(iter(settled))):
                sector, source[iid] = s, "bond_class"
        issuers[iid] = Issuer(iid, x["name"], x.get("short_name"), x.get("inn"), sector, bool(x.get("is_sovereign")),
                              x.get("group_id"), x.get("group_name"), x.get("is_state_owned"), x.get("country"))
    return Reference(snap.id, snap.business_date, snap.doc.get("schema_version"), issuers,
                     RatingBook(d.get("ratings") or (), bond_issuer), source)


def newest_static_id(sd: SnapshotDir) -> str | None:
    """The newest static bundle of the folder: the latest business date, then the latest known_at and cut time."""
    best = None
    for sid, m in sd.manifests().items():
        if m.get("kind") != "static":
            continue
        key = (m.get("business_date") or "", _utc(m.get("known_at")), _utc(m.get("cut_ts")), sid)
        if best is None or key > best:
            best = key
    return best[-1] if best else None


_CACHE: dict[str, Reference] = {}


def of(snap: Snapshot) -> Reference:
    if snap.id not in _CACHE:
        if len(_CACHE) >= 3:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[snap.id] = build(snap)
    return _CACHE[snap.id]


def facts(sd: SnapshotDir | None, fallback: StaticView | Snapshot) -> Reference:
    """The reference facts of the folder's newest static (module doc), else of ``fallback`` (the run's own static)."""
    own = fallback.snapshot if isinstance(fallback, StaticView) else fallback
    sid = newest_static_id(sd) if sd is not None else None
    if sid and sid != own.id:
        m = sd.manifest(sid) or {}
        if (m.get("business_date") or "") >= own.business_date:
            try:
                return of(sd.get(sid))
            except (OSError, ValueError, KeyError, SnapshotError) as e:
                log.warning("reference facts: newest static %s not read (%s), the run's own is used", sid[:12], e)
    return of(own)
