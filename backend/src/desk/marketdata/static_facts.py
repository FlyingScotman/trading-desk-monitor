"""Facts of a static snapshot that the concept pages show beside the runs (R23), into the store's read model
(migration 0011): per bond its display and size facts, the market's payment calendar, and the CBR meetings of the
market snapshot. None of it is a run input; it is rebuilt from the cached bundles when lost.

``load_static(con, snap)`` (``desk eod`` for the run's static snapshot, the autopilot for the newest cached one;
nothing when the snapshot is in already) writes:

* ``md_static_bond``: per active bond (not matured on the snapshot's date) ``bond_facts``: the issuer's short name
  (hub static ``Issuer.short_name``, HS1; else the readable legal name), its group (``group_name``, HS4, 1.1), sector
  (``sector``, HS3: a code of the hub's taxonomy, shown in Russian by ``SECTOR_LABELS``), the Latin name
  (``lat_name``, HS12, 1.1), the issue size (``issue_size``, the number of bonds MOEX lists: for OFZ the issue's
  declared volume, not what was placed) and the outstanding face (issue size × the face on the snapshot's date, in
  the face currency), the coupon frequency (``coupon_frequency``, else ``coupon_frequency_schedule``, HS7) and the
  current coupon rate (the period running on the date, else the last published rate);
* ``md_market_flow``: every payment of those bonds from the day after the snapshot's date for ``HORIZON_DAYS``: coupons
  (``amount_per_bond`` × issue size; a coupon MOEX has not fixed yet takes the last published amount, scaled by the
  face, and is ``est``), amortizations and maturities (``value_per_bond`` × issue size), scheduled put offers and calls
  (the face outstanding on the date × issue size: what could be redeemed, not what will be). ``market`` ``rub``: the
  bonds with a RUB face; ``fx``: the bonds with a foreign face (the replacement and FX bonds of Russian issuers on
  MOEX; a foreign sovereign or an issuer the hub places outside Russia is left out). Amounts are in the face
  currency and never converted: the pages sum one currency at a time;
* ``md_static_summary``: the calendar aggregated (``calendar``): RUB by day × type × kind (amount, count, and the same
  for the large issues, outstanding >= ``BIG_RUB``), RUB by month × sector × kind (also for the large issues), the large RUB
  redemptions and offers one by one, FX by day × currency × kind, by month × currency × issuer × kind, and the FX redemptions and offers one by one.
  The drill-down reads the flows (``flows``).

RUB types: ``PD`` (ОФЗ-ПД, -АД, -н), ``PK`` (ОФЗ-ПК), ``IN`` (ОФЗ-ИН), ``CORP_FIX`` / ``CORP_FLT`` (corporates by coupon
type), ``SUB`` (subfederal and municipal), ``OTHER`` (mortgage and securitization notes, CBR bonds, structured notes,
the rest). The newest ``KEEP`` static snapshots are kept; older ones leave with their rows.

``load_cbr(con, snap)`` replaces ``md_cbr_meeting`` by the market snapshot's ``cbr_meetings`` unless a newer snapshot's
are in.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .bundle import Snapshot

log = logging.getLogger(__name__)

VERSION = 2
"""Format of the rows written here: another value rebuilds them on the next load (2: an offer listed by two sources
counted once)."""
KEEP = 4
HORIZON_DAYS = 400
BIG_RUB = 10e9
"""A large RUB issue (the pages' «только крупные»): outstanding face of at least 10 bn RUB."""

SECTOR_LABELS = {
    "banks": "Банки", "leasing": "Лизинг", "oil_gas": "Нефть и газ", "metals_mining": "Металлы и добыча",
    "utilities": "Энергетика и ЖКХ", "telecom": "Телеком", "real_estate": "Девелопмент", "transport": "Транспорт",
    "retail": "Ритейл", "agro": "АПК", "chemicals": "Химия", "it": "IT", "financial_spv": "Финансовые SPV",
    "securitization_spv": "Секьюритизация", "sovereign": "Суверен", "subfederal": "Субфедеральные",
    "municipal": "Муниципальные", "supranational": "Наднациональные", "other": "Прочее",
}
"""The hub's sector codes (``Issuer.sector``, HS3) in Russian."""
RUB_TYPES = ("PD", "PK", "IN", "CORP_FIX", "CORP_FLT", "SUB", "OTHER")
TYPE_LABELS = {"PD": "ОФЗ-ПД", "PK": "ОФЗ-ПК", "IN": "ОФЗ-ИН", "CORP_FIX": "Корп. фикс", "CORP_FLT": "Корп. флоатеры",
               "SUB": "Субфед. и муни", "OTHER": "Прочие (ИЦБ, ЦБ, структурные)"}
KINDS = ("cpn", "amort", "mat", "put", "call")
FREQ_LETTER = {12: "M", 4: "Q", 2: "S", 1: "A"}
NOT_RUSSIAN = ("foreign_sovereign", "ifi")


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(Decimal(str(v)))
    except (InvalidOperation, ValueError):
        return None


def rub_type(i: dict[str, Any]) -> str:
    c, ct = i.get("bond_class"), i.get("coupon_type")
    if c in ("ofz_pd", "ofz_ad", "ofz_n"):
        return "PD"
    if c == "ofz_pk":
        return "PK"
    if c == "ofz_in":
        return "IN"
    if c == "corporate":
        return "CORP_FLT" if ct == "floating" else "CORP_FIX"
    if c in ("subfederal", "municipal"):
        return "SUB"
    return "OTHER"


def _face_unit(i: dict[str, Any]) -> str:
    return i.get("face_unit") or ("RUB" if i.get("currency_id") in (None, "SUR", "RUB") else i["currency_id"])


def _issuer_name(iss: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """(short display name, full name) of an issuer row."""
    if not iss:
        return None, None
    from ..publish.tdm_run import issuer_display  # the run's own readable name, one per issuer on every page

    full = iss.get("name")
    short = iss.get("short_name") or full
    return (issuer_display(short) if short else None), full


def bond_facts(data: dict[str, Any], as_of: date) -> dict[str, dict[str, Any]]:
    """Per active bond of the static data (not matured on ``as_of``): see the module doc."""
    issuers = {int(x["issuer_id"]): x for x in data.get("issuers") or ()}
    cpns: dict[str, list[dict]] = defaultdict(list)
    for c in data.get("coupon_periods") or ():
        cpns[c["secid"]].append(c)
    out = {}
    for i in data.get("instruments") or ():
        mat = i.get("maturity_date")
        if i.get("status") == "matured" or (mat and mat <= as_of.isoformat()):
            continue
        iss = issuers.get(int(i["issuer_id"])) if i.get("issuer_id") is not None else None
        short, full = _issuer_name(iss)
        unit = _face_unit(i)
        size, face = _num(i.get("issue_size")), _num(i.get("face_value"))
        freq = i.get("coupon_frequency") or i.get("coupon_frequency_schedule")
        rate = None
        d = as_of.isoformat()
        rows = sorted(cpns.get(i["secid"], ()), key=lambda c: c["pay_date"])
        cur = next((c for c in rows if c["pay_date"] > d and (c.get("start_date") or "") <= d), None)
        if cur is not None and cur.get("rate_pct") is not None:
            rate = _num(cur["rate_pct"])
        else:
            known = [c for c in rows if c.get("rate_pct") is not None and c["pay_date"] <= d]
            rate = _num(known[-1]["rate_pct"]) if known else None
        sector = (iss or {}).get("sector")
        out[i["secid"]] = {
            "isin": i.get("isin"), "name": i.get("name"), "short_name": i.get("short_name"), "lat_name": i.get("lat_name"),
            "bond_class": i.get("bond_class"), "coupon_type": i.get("coupon_type"), "face_unit": unit,
            "market": "rub" if unit == "RUB" else "fx", "type": rub_type(i) if unit == "RUB" else unit,
            "issuer_id": i.get("issuer_id"), "issuer": short, "issuer_full": full,
            "group": (iss or {}).get("group_name"), "group_id": (iss or {}).get("group_id"),
            "sector": sector, "sector_label": SECTOR_LABELS.get(sector, sector) if sector else None,
            "country": (iss or {}).get("country"), "state_owned": (iss or {}).get("is_state_owned"),
            "issue_size": size, "face": face, "outstanding": size * face if size is not None and face is not None else None,
            "freq": freq, "freq_letter": FREQ_LETTER.get(freq) if freq else None, "cpn_pct": rate,
            "maturity": mat, "perpetual": bool(i.get("is_perpetual")), "structured": bool(i.get("is_structured")),
            "foreign_issuer": i.get("bond_class") in NOT_RUSSIAN or ((iss or {}).get("country") not in (None, "RU")),
        }
    return out


def flows(data: dict[str, Any], facts: dict[str, dict[str, Any]], as_of: date,
          horizon_days: int = HORIZON_DAYS) -> list[dict[str, Any]]:
    """Payments of the bonds of ``facts`` in ``(as_of, as_of + horizon_days]`` (module doc); a foreign-face bond of a
    foreign issuer is left out."""
    lo, hi = as_of.isoformat(), (as_of + timedelta(days=horizon_days)).isoformat()
    out: list[dict[str, Any]] = []
    reds: dict[str, list[dict]] = defaultdict(list)
    for r in data.get("redemptions") or ():
        reds[r["secid"]].append(r)
    cpns: dict[str, list[dict]] = defaultdict(list)
    for c in data.get("coupon_periods") or ():
        cpns[c["secid"]].append(c)

    def keep(f: dict[str, Any]) -> bool:
        return not (f["market"] == "fx" and f["foreign_issuer"])

    def add(sid: str, f: dict[str, Any], d: str, kind: str, per_bond: float | None, est: bool = False) -> None:
        n = f["issue_size"]
        out.append({"pay_date": d, "secid": sid, "kind": kind, "market": f["market"], "type": f["type"],
                    "ccy": f["face_unit"], "per_bond": per_bond,
                    "amount": per_bond * n if per_bond is not None and n is not None else None, "est": est})

    for sid, f in facts.items():
        if not keep(f):
            continue
        rows = sorted(cpns.get(sid, ()), key=lambda c: c["pay_date"])
        last: tuple[float, float | None] | None = None  # (amount per bond, its face) of the last fixed coupon
        for c in rows:
            amt = _num(c.get("amount_per_bond"))
            if amt is not None and amt > 0:
                last = (amt, _num(c.get("face_value")))
            if not (lo < c["pay_date"] <= hi):
                continue
            if amt is not None:
                add(sid, f, c["pay_date"], "cpn", amt, c.get("status") != "known")
            elif last is not None:
                face = _num(c.get("face_value"))
                scale = face / last[1] if face and last[1] else 1.0
                add(sid, f, c["pay_date"], "cpn", last[0] * scale, True)
            else:
                add(sid, f, c["pay_date"], "cpn", None, True)
        for r in reds.get(sid, ()):
            if lo < r["date"] <= hi:
                add(sid, f, r["date"], "mat" if r["kind"] == "maturity" else "amort", _num(r.get("value_per_bond")))
    seen: set[tuple[str, str, str]] = set()
    for o in data.get("offers") or ():
        f = facts.get(o["secid"])
        d = o.get("offer_date")
        if f is None or not keep(f) or not d or not (lo < d <= hi) or o.get("status") != "scheduled" \
                or o.get("type") not in ("put", "call"):
            continue
        # one offer from two sources (cbonds and MOEX ISS: 557 of 4 403 scheduled offers of the static of
        # 21.10.2026) is one event: counted twice it doubled the possible volume of the market's offers
        if (key := (o["secid"], d, o["type"])) in seen:
            continue
        seen.add(key)
        face = f["face"]
        if face is not None:  # the face outstanding on the offer date: the face now less what is redeemed before
            face -= sum(_num(r.get("value_per_bond")) or 0.0 for r in reds.get(o["secid"], ())
                        if lo < r["date"] < d)
        add(o["secid"], f, d, o["type"], face, True)
    out.sort(key=lambda x: (x["pay_date"], x["secid"], x["kind"]))
    return out


def calendar(fl: list[dict[str, Any]], facts: dict[str, dict[str, Any]], as_of: date) -> dict[str, Any]:
    """The aggregates of ``md_static_summary.data.calendar`` (module doc). Rows are lists, field names in
    ``fields``; amounts in the face currency (RUB for ``rub``)."""
    rub: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0, 0.0, 0, 0.0])
    sec: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0, 0.0, 0])
    fx: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0, 0.0])
    fxi: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0])
    big, fx_events = [], []
    unknown = 0
    for x in fl:
        f = facts[x["secid"]]
        a = x["amount"]
        if a is None:
            unknown += 1
            continue
        if x["market"] == "rub":
            r = rub[(x["pay_date"], x["type"], x["kind"])]
            r[0] += a
            r[1] += 1
            if (f["outstanding"] or 0) >= BIG_RUB:
                r[2] += a
                r[3] += 1
            if x["est"]:
                r[4] += a
            s = sec[(x["pay_date"][:7], f["sector_label"] or ("ОФЗ" if x["type"] in ("PD", "PK", "IN") else "—"),
                     x["kind"])]
            s[0] += a
            s[1] += 1
            if (f["outstanding"] or 0) >= BIG_RUB:
                s[2] += a
                s[3] += 1
            if x["kind"] != "cpn" and (f["outstanding"] or 0) >= BIG_RUB:
                big.append([x["pay_date"], x["kind"], x["secid"], f["short_name"] or f["name"], x["type"], a])
        else:
            r = fx[(x["pay_date"], x["ccy"], x["kind"])]
            r[0] += a
            r[1] += 1
            if x["est"]:
                r[2] += a
            i = fxi[(x["pay_date"][:7], x["ccy"], f["issuer"] or "—", x["kind"])]
            i[0] += a
            i[1] += 1
            if x["kind"] != "cpn":
                fx_events.append([x["pay_date"], x["kind"], x["secid"], f["short_name"] or f["name"], f["issuer"],
                                  x["ccy"], a])
    rub_bonds = [f for f in facts.values() if f["market"] == "rub"]
    fx_bonds = [f for f in facts.values() if f["market"] == "fx" and not f["foreign_issuer"]]
    return {
        "asOf": as_of.isoformat(), "horizonEnd": (as_of + timedelta(days=HORIZON_DAYS)).isoformat(),
        "bigRub": BIG_RUB, "types": list(RUB_TYPES), "typeLabels": TYPE_LABELS,
        "fields": {"rub": ["date", "type", "kind", "amount", "count", "bigAmount", "bigCount", "estAmount"],
                   "rubSector": ["month", "sector", "kind", "amount", "count", "bigAmount", "bigCount"],
                   "rubBig": ["date", "kind", "secid", "name", "type", "amount"],
                   "fx": ["date", "ccy", "kind", "amount", "count", "estAmount"],
                   "fxIssuers": ["month", "ccy", "issuer", "kind", "amount", "count"],
                   "fxEvents": ["date", "kind", "secid", "name", "issuer", "ccy", "amount"]},
        "rub": [[d, t, k, round(v[0], 2), v[1], round(v[2], 2), v[3], round(v[4], 2)] for (d, t, k), v in sorted(rub.items())],
        "rubSector": [[m, s, k, round(v[0], 2), v[1], round(v[2], 2), v[3]] for (m, s, k), v in sorted(sec.items())],
        "rubBig": sorted(big),
        "fx": [[d, c, k, round(v[0], 2), v[1], round(v[2], 2)] for (d, c, k), v in sorted(fx.items())],
        "fxIssuers": [[m, c, i, k, round(v[0], 2), v[1]] for (m, c, i, k), v in sorted(fxi.items())],
        "fxEvents": sorted(fx_events),
        "ccys": sorted({f["face_unit"] for f in fx_bonds}),
        "coverage": {"rub_bonds": len(rub_bonds), "rub_without_size": sum(f["issue_size"] is None for f in rub_bonds),
                     "fx_bonds": len(fx_bonds), "fx_without_size": sum(f["issue_size"] is None for f in fx_bonds),
                     "flows": len(fl), "flows_unknown_amount": unknown,
                     "with_sector": sum(bool(f["sector"]) for f in facts.values()),
                     "with_group": sum(bool(f["group"]) for f in facts.values()),
                     "with_lat_name": sum(bool(f["lat_name"]) for f in facts.values()), "bonds": len(facts)},
    }


def _jsonb(x: Any) -> Jsonb:
    return Jsonb(x, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


def load_static(con: psycopg.Connection, snap: Snapshot) -> str:
    """The static snapshot's facts into the read model (module doc). Returns ``loaded``, ``cached`` (in already)
    or ``older`` (``KEEP`` newer snapshots are in: not needed)."""
    have = con.execute("SELECT version FROM md_static_summary WHERE snapshot_id = %s", (snap.id,)).fetchone()
    if have and have[0] == VERSION:
        return "cached"
    newer = con.execute("SELECT count(*) FROM md_static_summary WHERE business_date > %s AND snapshot_id <> %s",
                        (snap.business_date, snap.id)).fetchone()[0]
    if newer >= KEEP:
        return "older"
    as_of = date.fromisoformat(snap.business_date)
    facts = bond_facts(snap.data, as_of)
    fl = flows(snap.data, facts, as_of)
    cal = calendar(fl, facts, as_of)
    with con.transaction(), con.cursor() as cur:
        cur.execute("DELETE FROM md_static_summary WHERE snapshot_id = %s", (snap.id,))
        cur.execute("INSERT INTO md_static_summary (snapshot_id, business_date, version, data) VALUES (%s, %s, %s, %s)",
                    (snap.id, as_of, VERSION, _jsonb({"calendar": cal, "schema_version": snap.doc.get("schema_version")})))
        with cur.copy("COPY md_static_bond (snapshot_id, secid, data) FROM STDIN") as cp:
            for sid, f in facts.items():
                cp.write_row((snap.id, sid, json.dumps(f, ensure_ascii=False)))
        with cur.copy("COPY md_market_flow (snapshot_id, pay_date, secid, kind, market, type, ccy, amount, per_bond, est)"
                      " FROM STDIN") as cp:
            for x in fl:
                cp.write_row((snap.id, x["pay_date"], x["secid"], x["kind"], x["market"], x["type"], x["ccy"],
                              x["amount"], x["per_bond"], x["est"]))
        cur.execute("DELETE FROM md_static_summary WHERE snapshot_id IN (SELECT snapshot_id FROM md_static_summary"
                    " ORDER BY business_date DESC, loaded_at DESC OFFSET %s)", (KEEP,))
    log.info("static facts %s (%s): %d bonds, %d flows", snap.id[:12], snap.business_date, len(facts), len(fl))
    return "loaded"


def load_cbr(con: psycopg.Connection, snap: Snapshot) -> str:
    """``md_cbr_meeting`` from the market snapshot's ``cbr_meetings`` (module doc): ``loaded``, ``older`` or
    ``none`` (the snapshot has none)."""
    rows = snap.data.get("cbr_meetings") or []
    if not rows:
        return "none"
    cur = con.execute("SELECT max(business_date), max(snapshot_id) FILTER (WHERE business_date ="
                      " (SELECT max(business_date) FROM md_cbr_meeting)) FROM md_cbr_meeting").fetchone()
    if cur[0] is not None and (cur[0].isoformat() > snap.business_date
                               or (cur[0].isoformat() == snap.business_date and cur[1] == snap.id)):
        return "older"
    with con.transaction():
        con.execute("DELETE FROM md_cbr_meeting")
        with con.cursor() as c, c.copy("COPY md_cbr_meeting (meeting_date, effective_date, is_estimated,"
                                       " is_extraordinary, decision, decision_rate_pct, prev_rate_pct, snapshot_id,"
                                       " business_date) FROM STDIN") as cp:
            for m in {r["meeting_date"]: r for r in rows}.values():
                cp.write_row((m["meeting_date"], m.get("effective_date"), bool(m.get("is_estimated")),
                              bool(m.get("is_extraordinary")), m.get("decision"), _num(m.get("decision_rate_pct")),
                              _num(m.get("prev_rate_pct")), snap.id, snap.business_date))
    return "loaded"


def static_for(con: psycopg.Connection, as_of: date) -> dict[str, Any] | None:
    """The read model's static snapshot for a run of ``as_of``: the newest loaded on or before it, else the oldest
    loaded (a run older than every kept snapshot). {snapshot_id, business_date, data} or None."""
    row = con.execute("SELECT snapshot_id, business_date, data FROM md_static_summary WHERE business_date <= %s"
                      " ORDER BY business_date DESC, loaded_at DESC LIMIT 1", (as_of,)).fetchone()
    if row is None:
        row = con.execute("SELECT snapshot_id, business_date, data FROM md_static_summary"
                          " ORDER BY business_date, loaded_at LIMIT 1").fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {"snapshot_id": row[0], "business_date": row[1], "data": row[2]}
