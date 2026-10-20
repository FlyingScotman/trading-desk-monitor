"""R9 static audit, desk side (D1-D6, D8, D10): what a position shows of the static data, on the golden fixtures and
synthetic rows: MOEX's full name, a coupon for every bond (a floater's amount as a rate, else index + margin), the hub's
coupon types, a readable issuer, "нет данных" for what the hub lacks, unmapped bonds under the report's issuer and
listed by reason; and snapshots of contract 1.x with fields the desk does not know."""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from desk.inbox import static_index
from desk.marketdata import cache
from desk.marketdata.adapter import StaticView
from desk.marketdata.bundle import SnapshotDir, SnapshotError, parse_bundle, supported_schema
from desk.orchestrator import prepare, value
from desk.publish import tdm_run
from desk.valuation.bonds import coupon_view, implied_frequency

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
DATE = "2026-10-15"
STATIC = "4f173275220b492c558acad982351bac01fe73baea6d50dcb0fcc38400953c4a"
FLOATER = "RU000A109XR1"  # KEY_RATE + 175 bp; running coupon 10.36 RUB on face 800 over 30 days, no rate


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


def _bundle(folder: Path, doc: dict) -> tuple[str, bytes]:
    raw = json.dumps(doc, ensure_ascii=False).encode()
    sid = hashlib.sha256(raw).hexdigest()
    body = gzip.compress(raw)
    (folder / f"{sid}.json.gz").write_bytes(body)
    return sid, body


def _static_doc() -> dict:
    return json.loads(gzip.decompress((FIX / f"{STATIC}.json.gz").read_bytes()))


# ---- D10: market_snapshot/1.x

def test_schema_versions():
    assert supported_schema("market_snapshot/1.0") and supported_schema("market_snapshot/1.1")
    assert supported_schema("market_snapshot/1.12")
    for v in ("market_snapshot/2.0", "market_snapshot/1", "market_snapshot/1.x", "market_snapshot/10.1", None, 1.1):
        assert not supported_schema(v), v


def test_a_1_1_bundle_with_unknown_fields_is_read(tmp_path):
    doc = _static_doc()
    doc["schema_version"] = "market_snapshot/1.1"
    doc["producer_extra"] = {"anything": 1}  # unknown envelope field
    for i in doc["data"]["instruments"]:
        i["lat_name"], i["coupon_frequency_schedule"] = "Latin name", 4  # unknown instrument fields (HS6, HS7)
    for x in doc["data"]["issuers"]:
        x["okved"], x["group_id"] = "64.19", "G1"
    doc["data"]["ratings"] = [{"issuer_id": 1, "agency": "acra", "rating": "AAA(RU)"}]  # an unknown dataset
    sid, body = _bundle(tmp_path, doc)

    assert cache.verify(body, sid)["schema_version"] == "market_snapshot/1.1"
    snap = parse_bundle(body, sid)
    view, ref = StaticView(snap), StaticView(SnapshotDir(FIX).get(STATIC))
    assert {s: dataclasses.replace(b, lat_name=None, coupon_frequency_schedule=None) for s, b in view.bonds.items()} \
        == ref.bonds  # the 1.1 fields are read (test_hub_static_1_1), nothing else changes
    assert view.bonds[FLOATER].lat_name == "Latin name" and view.bonds[FLOATER].coupon_frequency_schedule == 4
    assert {i: dataclasses.replace(x, okved=None, group_id=None) for i, x in view.issuers.items()} == ref.issuers
    assert [(r.scope, r.issuer_id, r.rating) for r in view.ratings] == [("issuer", 1, "AAA(RU)")]
    assert ref.ratings is None  # a 1.0 snapshot has no ratings: "нет данных", not "без рейтинга"
    assert SnapshotDir(tmp_path).get(sid).doc["schema_version"] == "market_snapshot/1.1"

    refs = static_index.static_snapshots(tmp_path)  # the inbox reads it too
    assert [r.id for r in refs] == [sid]
    idx = static_index.Instruments(static_index.build_index(tmp_path, tmp_path / "index.json"))
    assert idx.lookup("isin", FLOATER)["moex_secid"] == FLOATER


def test_another_major_is_refused(tmp_path):
    doc = _static_doc()
    doc["schema_version"] = "market_snapshot/2.0"
    sid, body = _bundle(tmp_path, doc)
    with pytest.raises(SnapshotError, match="need market_snapshot/1.x"):
        cache.verify(body, sid)
    with pytest.raises(SnapshotError, match="need market_snapshot/1.x"):
        parse_bundle(body, sid)
    assert static_index.static_snapshots(tmp_path) == []  # not ours to read: no index, no calendar from it
    with pytest.raises(ValueError, match="schema_version"):
        static_index._data(static_index.StaticRef(sid, doc["business_date"], tmp_path / f"{sid}.json.gz"))


# ---- D2, D3: issuers

@pytest.mark.parametrize("legal, shown", [
    ('открытое акционерное общество "Российские железные дороги"', "Российские железные дороги"),
    ('АКЦИОНЕРНОЕ ОБЩЕСТВО "АЛЬФА-БАНК"', "Альфа-Банк"),
    ("Банк ВТБ (публичное акционерное общество)", "Банк ВТБ"),
    ('ПАО "Газпром нефть"', "Газпром нефть"),
    ("Банк ВТБ (ПАО)", "Банк ВТБ"),
    ('ООО "Газпром капитал"', "Газпром капитал"),
    ('ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "СПЕЦИАЛИЗИРОВАННОЕ ФИНАНСОВОЕ ОБЩЕСТВО ВТБ РКС 1"', "СФО ВТБ РКС 1"),
    ('ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО "ГТЛК"', "ГТЛК"),
    ("Министерство финансов Российской Федерации", "Министерство финансов Российской Федерации"),
    ('Государственная корпорация развития «ВЭБ.РФ»', "ВЭБ.РФ"),
    ("АО", "АО"),  # nothing left: the name as it is
])
def test_issuer_display(legal, shown):
    assert tdm_run.issuer_display(legal) == shown


# ---- D1, D4, D5, D8: the position row

def _row(ctx, secid: str, terms=None, x: dict | None = None):
    o = value(ctx, [(secid, 10.0)], position_ids=["P1"])[0]
    if terms is not None:
        o.terms = terms
    c = tdm_run.run_content(ctx, [("P1", "CORP", o)], None, {})
    return c.rows[0], {i["issuer"]: i for i in c.reference["issuers"]}


def test_floater_row(ctx):
    row, issuers = _row(ctx, FLOATER)
    t = ctx.static.bonds[FLOATER]
    assert row["name"] == t.name == "Акрон (ПАО) БО-001P-05" and row["extra"]["short_name"] == t.short_name
    assert row["instrument_id"] == FLOATER
    assert row["coupon_type"] == "float" and row["coupon"] == pytest.approx(10.36 * 365 * 100 / (800 * 30), abs=1e-4)
    assert (row["index_name"], row["margin"], row["extra"]["coupon_basis"]) == ("КС", 1.75, "amount")
    iss = issuers[row["issuer"]]
    assert (iss["name"], iss["issuer_group"], iss["sector"], iss["rating"]) == ("Акрон", "Акрон", "нет данных",
                                                                               "нет данных")


def test_floater_without_a_published_coupon_is_projected(ctx):
    t = ctx.static.bonds[FLOATER]
    bare = tuple(dataclasses.replace(c, amount_per_bond=None, rate_pct=None, status="unknown")
                 if c.pay_date > ctx.business_date else c for c in t.coupons)
    row, _ = _row(ctx, FLOATER, dataclasses.replace(t, coupons=bare))
    assert row["coupon"] == pytest.approx(14.0 + 1.75) and row["extra"]["coupon_basis"] == "projected"
    assert coupon_view(dataclasses.replace(t, coupons=bare), ctx.business_date).pct is None  # no fixing: no figure
    capped = dataclasses.replace(t, coupons=bare, floater_formula={**t.floater_formula, "cap_pct": "15.0"})
    assert coupon_view(capped, ctx.business_date, lambda _: 14.0).pct == 15.0


def test_coupon_types_and_rungs(ctx):
    row, _ = _row(ctx, "SU52005RMFS4")  # OFZ-IN: a fallback rung, its real coupon still shown
    assert row["extra"]["quality"] != "exact" and row["coupon_type"] == "indexed_face" and row["coupon"] == 2.5
    row, _ = _row(ctx, "RU000A10GCT1")
    assert row["coupon_type"] == "unknown" and row["coupon"] == 16.25
    row, _ = _row(ctx, "RU000A10BRM5")  # USD face
    assert row["coupon"] == 7.75 and row["coupon_type"] == "fix"
    t = ctx.static.bonds["SU26238RMFS4"]
    assert coupon_view(dataclasses.replace(t, coupon_type="zero", coupons=()), ctx.business_date).basis == "zero"
    gap = tuple(dataclasses.replace(c, amount_per_bond=None, rate_pct=None) if c.pay_date > ctx.business_date else c
                for c in t.coupons)
    assert coupon_view(dataclasses.replace(t, coupons=gap), ctx.business_date) == \
        type(coupon_view(t, ctx.business_date))(7.1, "assumed")


def test_frequency_from_the_schedule(ctx):
    t = ctx.static.bonds[FLOATER]
    assert implied_frequency(t) == 12
    row, _ = _row(ctx, FLOATER, dataclasses.replace(t, coupon_frequency=None))
    assert row["coupon_freq"] == 12 and row["extra"]["coupon_freq_source"] == "schedule"
    row, _ = _row(ctx, FLOATER)
    assert row["coupon_freq"] == 12 and "coupon_freq_source" not in row["extra"]


def test_tdm_store_gets_fix_or_float(ctx, tmp_path):
    o = value(ctx, [("RU000A10GCT1", 10.0)], position_ids=["P1"])[0]
    _, status, _ = tdm_run.publish(str(tmp_path / "tdm.db"), ctx, [("P1", "CORP", o)], prev=None)
    assert status == "complete"  # tdm's CHECK (fix, float) holds: "unknown" is written as fix


# ---- D6: unmapped bonds

def test_unmapped_bond_shows_the_report(ctx):
    from desk.store import PositionInputs
    from desk.valuation.positions import merge, plan

    rows = [("B1", "CORP", None, None), ("B2", "CORP", None, None)]
    inp = PositionInputs(rows, [], {"CORP": date(2026, 10, 15)}, datetime.now(UTC), "0" * 64, {
        "B1": {"asset_class": "bond", "ccy": "RUB", "source_instrument_name": "SIBN 2046", "isin": "RU000A0JWRE5",
               "mv_rub": 1e6, "x": {"x_issuer": 'ПАО "Газпром нефть"'}},
        "B2": {"asset_class": "bond", "ccy": "RUB", "isin": "XS0000000001", "mv_rub": 5e5}}, {})
    p = plan(ctx, inp)
    positions = merge(p, value(ctx, p.requests, position_ids=p.position_ids))
    c = tdm_run.run_content(ctx, positions, None, {}, inp)
    rows = {r["position_id"]: r for r in c.rows}
    issuers = {i["issuer"]: i for i in c.reference["issuers"]}
    assert rows["B1"]["name"] == "SIBN 2046 (из отчёта)" and rows["B1"]["isin"] == "RU000A0JWRE5"
    assert issuers[rows["B1"]["issuer"]]["name"] == "Газпром нефть"
    assert rows["B2"]["name"] == "XS0000000001 (из отчёта)" and rows["B2"]["issuer"] == "Не сопоставлено"


def test_unmapped_reasons():
    rows = [(None, {"asset_class": "bond", "isin": FLOATER}),  # resolves
            ("SU26238RMFS4", None),  # a legacy row by secid
            (None, {"asset_class": "bond", "isin": "RU000A0JWRE5"}),
            (None, {"asset_class": "bond", "isin": "RU000A0JWRF2"}),
            (None, {"asset_class": "bond", "isin": "XS0000000001"}),
            (None, {"asset_class": "bond", "source_instrument_name": "no codes"}),
            (None, {"asset_class": "fx", "ccy": "USD"})]
    u = static_index.unmapped_bonds(FIX, rows, date(2026, 10, 15))
    assert u["static_snapshot"] == STATIC and u["count"] == 4
    assert u["by_reason"] == {"not_in_static": 2, "not_on_moex": 1, "no_identifier": 1}
    assert u["isins"] == [{"isin": "RU000A0JWRE5", "reason": "not_in_static"},
                          {"isin": "RU000A0JWRF2", "reason": "not_in_static"},
                          {"isin": "XS0000000001", "reason": "not_on_moex"}]
    keys = {"moex_secid": {"A": "A", "B": "B"}, "isin": {"RU1": None}, "regnum": {}}
    assert static_index.unmapped_reason(keys, {"isin": "RU1"}) == "ambiguous"
    assert static_index.unmapped_reason(keys, {"isin": "RU1", "moex_secid": "A"}) is None
    from desk.inbox.scan import unmapped_text
    assert unmapped_text(u, limit=1) == ("unmapped bonds 4 (no_identifier 1, not_in_static 2, not_on_moex 1): "
                                         "RU000A0JWRE5 not_in_static and 2 more")
