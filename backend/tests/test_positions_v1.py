"""Slice S7a: positions of the private contract v1 (valuation.positions) on the golden fixtures: face amounts,
re-resolution, unmapped bonds (reported_value), FX and OTC rows; published into the desk store and read back
through the API and window.DESK. Synthetic rows only."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from rub_fixed_income import RISK_BUCKETS
from run_checks import consistency, content

from desk import store
from desk.api import create_app
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value
from desk.publish import desk_run, tdm_run
from desk.scenario.exact import reprice_run
from desk.store import PositionInputs
from desk.valuation.positions import merge, plan

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
DATE = "2026-10-15"
USD = 83.76  # MOEX 19:00 on the fixture date (R26: FX rows and bonds at the market rate; CBR 83.9497)
FLOATER = "RU000A109XR1"  # amortized: initial face 1000, outstanding 800


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


def inputs(rows: list[tuple], extra: dict | None = None) -> PositionInputs:
    return PositionInputs(rows, [], {b: date.fromisoformat(DATE) for _, b, *_ in rows},
                          datetime.now(UTC), "0" * 64, extra or {},
                          {"CORP": {"source": "positions_daily", "report_date": date(2026, 10, 16), "file_name": "x.xlsx",
                                    "inbox_file_id": 1, "sha256": "1" * 64}})


def bond(**kw) -> dict:
    return {"asset_class": "bond", "ccy": "RUB", **kw}


def run(ctx, inp: PositionInputs):
    p = plan(ctx, inp)
    return p, merge(p, value(ctx, p.requests, position_ids=p.position_ids))


def one(ctx, x: dict, secid: str | None = None, qty: float | None = None):
    _, [(_, _, o)] = run(ctx, inputs([("P1", "CORP", secid, qty)], {"P1": x}))
    return o


def test_legacy_rows_unchanged(ctx):
    rows = [("A", "OFZ", "SU26238RMFS4", 10.0), ("B", "CORP", FLOATER, -5.0)]
    p, out = run(ctx, inputs(rows))
    assert p.requests == [("SU26238RMFS4", 10.0), (FLOATER, -5.0)] and not p.direct and not p.notes
    ref = value(ctx, [r[2:] for r in rows])
    assert [(pid, b, o.valuation.pv_rub, o.valuation.assumptions) for pid, b, o in out] \
        == [("A", "OFZ", ref[0].valuation.pv_rub, ref[0].valuation.assumptions),
            ("B", "CORP", ref[1].valuation.pv_rub, ref[1].valuation.assumptions)]


@pytest.mark.parametrize("field, amount", [("face_amount_ccy", 10_000.0), ("face_outstanding_ccy", 8_000.0)])
def test_face_to_number_of_bonds(ctx, field, amount):
    o = one(ctx, bond(**{field: amount}), FLOATER)
    assert o.valuation.quantity == pytest.approx(10.0)
    assert any(a.startswith(f"number of bonds 10 = {field}") for a in o.valuation.assumptions)


def test_initial_face_is_the_static_initial_face_value(ctx, monkeypatch):
    o = one(ctx, bond(face_amount_ccy=10_000.0), "SU52005RMFS4")  # OFZ-IN: indexed face 1303.17, initial 1000
    assert o.valuation.quantity == pytest.approx(10.0)
    t = ctx.static.bonds[FLOATER]
    monkeypatch.setitem(ctx.static.bonds, FLOATER, dataclasses.replace(t, initial_face_value=None))
    o = one(ctx, bond(face_amount_ccy=10_000.0), FLOATER)  # without the field: from the redemptions, said so
    assert o.valuation.quantity == pytest.approx(10.0)
    assert any("no initial_face_value" in a for a in o.valuation.assumptions)


def test_decimal_and_string_inputs(ctx):
    for v in (Decimal(10000), "10000"):
        assert one(ctx, bond(face_amount_ccy=v, ccy="rub"), FLOATER).valuation.quantity == pytest.approx(10.0)
    o = one(ctx, {"asset_class": "fx", "ccy": "usd", "amount_ccy": Decimal(-1000), "value_date": "2027-01-15"})
    assert (o.valuation.quality, o.valuation.pv_rub, o.valuation.quantity) == ("assumed", 0.0, -1000.0)
    o = one(ctx, bond(source_instrument_id="XS1", mv_rub="5000", mark_price_pct=Decimal("98.5")), None, Decimal(3))
    assert o.valuation.pv_rub == 5000.0 and o.valuation.quantity == 3.0


def test_direct_row_without_a_rate_shows_the_market_value(ctx):
    x = bond(source_instrument_id="XS9", ccy="XYZ", mv_rub=5e5, face_amount_ccy=1e6)
    o = one(ctx, x)
    row = tdm_run.direct_row(ctx, "CORP", "P1", o, x, None)
    assert (row["ccy"], row["notional"], row["notional_rub"], row["mv_rub"]) == ("RUB", 5e5, 5e5, 5e5)


def test_quantity_face_mismatch_keeps_the_quantity(ctx):
    o = one(ctx, bond(face_amount_ccy=20_000.0, face_outstanding_ccy=8_000.0), FLOATER, 10.0)
    assert o.valuation.quantity == 10.0
    notes = [a for a in o.valuation.assumptions if a.startswith("quantity_face_mismatch")]
    assert len(notes) == 1 and "face_amount_ccy 20" in notes[0]  # face_outstanding agrees with the quantity


def test_re_resolution_by_isin_and_regnum(ctx):
    for x in (bond(isin="RU000A1038V6"), bond(regnum="26238RMFS"), bond(moex_secid="NOSUCH", isin="RU000A1038V6")):
        o = one(ctx, x, None, 10.0)
        assert o.secid == "SU26238RMFS4" and o.valuation.quality == "exact"
        assert o.valuation.assumptions[0].startswith("instrument resolved by") and DATE in o.valuation.assumptions[0]


def test_unmapped_with_mv_rub(ctx):
    o = one(ctx, bond(source_instrument_id="XS000", mv_rub=1_234_567.0, mark_price_pct=98.0), None, 10.0)
    v = o.valuation
    assert (v.quality, v.pv_rub, v.dv01_rub, v.spread01_rub) == ("reported_value", 1_234_567.0, 0.0, 0.0)
    assert o.reason == "unmapped_instrument: XS000" and o.secid is None


def test_unmapped_with_mark_price(ctx):
    o = one(ctx, bond(source_instrument_id="XS001", ccy="USD", mark_price_pct=95.0, accrued_ccy=10.0,
                      face_outstanding_ccy=100_000.0))
    assert o.valuation.quality == "reported_value"
    assert o.valuation.pv_rub == pytest.approx((95_000.0 + 10.0) * USD)


def test_unmapped_with_nothing_is_not_valued(ctx):
    o = one(ctx, bond(source_instrument_id="XS002", face_amount_ccy=1e6), "NOSUCH")
    assert o.valuation is None and o.reason == "unmapped_instrument" and "XS002" in o.detail


def test_fx_spot_forward_and_rub(ctx):
    """A spot or cash row is worth its amount at the rate of the day; a forward (value_date after the business date)
    only its reported mv_rub, else 0: its amount is FX exposure (the OCP), never MV."""
    p, out = run(ctx, inputs([("S", "FX", None, None), ("F", "FX", None, None), ("R", "FX", None, None),
                              ("T", "FX", None, None), ("N", "FX", None, None)], {
        "S": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 1000.0},
        "F": {"asset_class": "fx", "ccy": "USD", "amount_ccy": -1000.0, "value_date": date(2027, 1, 15)},
        "R": {"asset_class": "fx", "ccy": "RUB", "amount_ccy": 5e6, "value_date": date(2026, 10, 15)},
        "T": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 5e6, "value_date": date(2026, 10, 16)},
        "N": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 5e6, "value_date": "2027-04-15", "mv_rub": "-1250000"}}))
    assert not p.requests
    s, f, r, t, n = (o.valuation for _, _, o in out)
    assert (s.quality, s.pv_rub, s.dv01_rub) == ("market_value", pytest.approx(1000 * USD), 0.0)
    assert (f.quality, f.pv_rub, f.quantity) == ("assumed", 0.0, -1000.0)
    assert "PV assumed 0" in f.assumptions[-1] and "not in MV" in f.assumptions[-1]
    assert (r.quality, r.pv_rub) == ("market_value", 5e6)  # value date = business date: cash
    assert (t.quality, t.pv_rub) == ("assumed", 0.0)  # T+1 is after the business date: a forward
    assert (n.quality, n.pv_rub, n.quantity, n.face_rub) == ("reported_value", -1.25e6, 5e6, USD)
    assert "mv_rub" in n.assumptions[-1]


def test_otc_without_mv_rub_not_valued(ctx):
    o = one(ctx, {"asset_class": "otc", "source_instrument_id": "IRS-1", "ccy": "RUB", "risk_dv01_rub": -5.0})
    assert o.valuation is None and o.reason == "no_reported_value" and "IRS-1" in o.detail


def test_otc_at_reported_value_with_bucketed_risk(ctx):
    """I3: mv_rub is the PV (reported_value), risk_dv01_rub whole in the bucket nearest maturity_date, CS01 as is."""
    o = one(ctx, {"asset_class": "otc", "source_instrument_id": "IRS-2", "ccy": "RUB", "mv_rub": "125000.5",
                  "maturity_date": "2031-01-10", "risk_dv01_rub": "-850.25", "risk_cs01_rub": -12.0})
    v = o.valuation
    assert (v.quality, v.pv_rub, v.dv01_rub, v.spread01_rub, v.spread_kind) == (
        "reported_value", 125000.5, -850.25, -12.0, "credit")  # 4.24 years: the 5Y pillar is nearest
    assert v.buckets["5Y"] == -850.25 and sum(1 for x in v.buckets.values() if x) == 1
    assert v.risk() == {"RUB RUONIA": v.buckets}  # a RUB row: on RUONIA, the RUB discount curve
    assert any("bucket 5Y of RUB RUONIA" in a and "approximate" in a for a in v.assumptions)
    cny = one(ctx, {"asset_class": "otc", "source_instrument_id": "XCCY-1", "ccy": "CNY", "mv_rub": 1.0,
                    "maturity_date": "2031-01-10", "risk_dv01_rub": -40.0})  # R8: on the curve of its currency
    assert cny.valuation.risk() == {"CNY": {**dict.fromkeys(RISK_BUCKETS, 0.0), "5Y": -40.0}}
    assert cny.valuation.dv01_rub == -40.0 and any("bucket 5Y of CNY" in a for a in cny.valuation.assumptions)
    short = one(ctx, {"asset_class": "other", "source_instrument_id": "X", "ccy": "RUB", "mv_rub": 1.0,
                      "risk_dv01_rub": 3.0})  # no maturity: the shortest bucket, flagged
    assert short.valuation.buckets["1M"] == 3.0 and any("no maturity_date" in a for a in short.valuation.assumptions)
    past = one(ctx, {"asset_class": "otc", "source_instrument_id": "Y", "ccy": "RUB", "mv_rub": 1.0,
                     "maturity_date": date(2026, 1, 1), "risk_dv01_rub": 3.0})
    assert past.valuation.buckets["1M"] == 3.0
    near = one(ctx, {"asset_class": "otc", "source_instrument_id": "Z", "ccy": "RUB", "mv_rub": 1.0,
                     "maturity_date": date(2027, 1, 20), "risk_dv01_rub": 3.0})  # 0.27 years: 3M
    assert near.valuation.buckets["3M"] == 3.0
    bare = one(ctx, {"asset_class": "otc", "source_instrument_id": "W", "ccy": "RUB", "mv_rub": -7.0})
    assert bare.valuation.dv01_rub == 0.0 and bare.valuation.spread01_rub == 0.0
    weird = one(ctx, {"asset_class": "swaption", "source_instrument_id": "V", "ccy": "RUB", "mv_rub": 2.0})
    assert weird.valuation.quality == "reported_value"  # an asset class nobody knows: valued as other, no crash


def test_unmapped_bond_takes_the_reported_risk(ctx):
    o = one(ctx, bond(source_instrument_id="XS009", mv_rub=1e6, maturity_date=date(2036, 3, 1), risk_dv01_rub=-900.0,
                      risk_cs01_rub=-800.0))
    v = o.valuation
    assert v.quality == "reported_value" and v.buckets["10Y"] == -900.0 and v.spread01_rub == -800.0


def test_subfederal_segment_is_published(ctx):
    """A subfederal or municipal bond has segment Суб (the Desk Monitor gives it a chip from the data)."""
    _, [(pid, book, o)] = run(ctx, inputs([("P1", "CORP", FLOATER, 10.0)]))
    o.terms = dataclasses.replace(o.terms, bond_class="subfederal")
    row = tdm_run.run_content(ctx, [(pid, book, o)], None, {}).rows[0]
    assert (row["segment"], row["asset_class"]) == ("Суб", "Субфедеральные")


def test_publish_api_and_compat(ctx, store_url):
    rows = [("L1", "OFZ", "SU26238RMFS4", 10.0), ("B1", "CORP", None, None), ("B2", "CORP", None, 10.0),
            ("B3", "CORP", None, None), ("B4", "CORP", None, None), ("FX1", "TSY", None, None),
            ("FX2", "CORP", None, None), ("FX3", "CORP", None, None), ("O1", "CORP", None, None),
            ("O2", "CORP", None, None)]
    inp = inputs(rows, {
        "B1": bond(source_instrument_id="RU000A109XR1", isin=FLOATER, face_outstanding_ccy=8_000.0,
                   mark_price_pct=99.0, mv_rub=8000.0, src="R12", x={"x_desk": "a"}),
        "B2": bond(source_instrument_id="XS000", source_instrument_name="Bond abroad", mv_rub=1e6),
        "B3": bond(source_instrument_id="XS001", ccy="USD", mark_price_pct=95.0, face_amount_ccy=1e5,
                   maturity_date=date(2029, 6, 1), risk_dv01_rub=-2400.0),
        "B4": bond(source_instrument_id="XS002"),
        "FX1": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 1000.0},
        "FX2": {"asset_class": "fx", "ccy": "USD", "amount_ccy": -400.0, "value_date": date(2027, 1, 15)},
        "FX3": {"asset_class": "fx", "ccy": "RUB", "amount_ccy": 5e6},
        "O1": {"asset_class": "otc", "source_instrument_id": "IRS-1"},
        "O2": {"asset_class": "otc", "source_instrument_id": "IRS-2", "source_instrument_name": "IRS 5Y", "ccy": "RUB",
               "mv_rub": 5e5, "maturity_date": date(2031, 1, 10), "risk_dv01_rub": -850.0}})
    _, positions = run(ctx, inp)
    with store.connect(store_url) as con:
        rid, status, warnings = desk_run.publish(con, ctx, positions, prev=None, inputs=inp)
    assert status == "complete" and any("B4" in w and "unmapped_instrument" in w for w in warnings)
    assert consistency(content(ctx, positions, inputs=inp)) == []  # the run content hangs together

    c = TestClient(create_app(store_url))
    got = {p["position_id"]: p for p in c.get(f"/api/v1/runs/{rid}/positions").json()["positions"]}
    assert [got[k]["quality"] for k, *_ in rows] == ["exact", "assumed", "reported_value", "reported_value", None,
                                                     "market_value", "assumed", "market_value", None,
                                                     "reported_value"]
    assert (got["B1"]["secid"], got["B1"]["quantity"]) == (FLOATER, pytest.approx(10.0))
    b1 = got["B1"]["fact"]["extra"]
    assert b1["reported"] == {"asset_class": "bond", "ccy": "RUB", "source_instrument_id": FLOATER, "isin": FLOATER,
                              "face_outstanding_ccy": 8000.0, "mark_price_pct": 99.0, "mv_rub": 8000.0, "src": "R12",
                              "x": {"x_desk": "a"}}
    assert got["FX1"]["secid"] is None and got["FX1"]["fact"]["fx_delta"] == {"USD": pytest.approx(1000.0)}
    assert got["FX3"]["fact"].get("fx_delta") is None and got["FX2"]["fact"]["maturity"] == "2027-01-15"
    fx2 = got["FX2"]["fact"]  # a forward: no MV, its amount only in the FX delta (the OCP) and the notional
    assert (got["FX2"]["pv_rub"], fx2["mv_rub"], fx2["fx_delta"], fx2["notional"]) == (0.0, 0.0, {"USD": -400.0},
                                                                                      -400.0)
    assert got["B2"]["fact"]["name"] == "Bond abroad (из отчёта)" and got["B2"]["dv01_rub"] == 0.0
    assert got["O1"]["reason"] == "no_reported_value: otc IRS-1: no mv_rub" and got["O1"]["fact"] is None
    o2 = got["O2"]["fact"]
    assert (got["O2"]["pv_rub"], got["O2"]["dv01_rub"], o2["instr_type"], o2["segment"]) == (5e5, -850.0, "OTC",
                                                                                           "Деривативы")
    assert o2["risk"] == {"RUB RUONIA": {**dict.fromkeys(o2["risk"]["RUB RUONIA"], 0.0), "5Y": -850.0}}
    assert o2["extra"]["quality"] == "reported_value" and o2["maturity"] == "2031-01-10" and o2["name"] == "IRS 5Y"
    desk = c.get(f"/api/v1/compat/desk?run_id={rid}").json()  # the Desk Monitor: O2 valued, O1 listed without PV
    dp = {p["id"]: p for p in desk["positions"]}
    assert dp["O2"]["mvRub"] == 5e5 and dp["O2"]["risk"]["RUB RUONIA"][6] == -850.0 and dp["O2"]["segment"] == "Деривативы"
    # R8: the USD bond's reported DV01 on the USD curve, which the Desk Monitor gets without points
    assert got["B3"]["fact"]["risk"] == {"USD": {**dict.fromkeys(RISK_BUCKETS, 0.0), "3Y": -2400.0}}
    assert dp["B3"]["risk"] == {"USD": [0.0] * 5 + [-2400.0] + [0.0] * 5}
    assert set(desk["CURVES"]) >= {c for p in desk["positions"] for c in p["risk"]} | {"USD"}
    assert desk["CURVES"]["USD"] == {"name": "USD: кривой нет, риск по дюрации", "ccy": "USD", "kind": "bond",
                                     "pts": [None] * 11} and desk["CURVE_MOVES"]["USD"] == [0.0] * 11
    assert dp["O1"]["notValued"] and dp["O1"]["mvRub"] is None and dp["O1"]["extra"]["quality"] == "not_valued"
    assert dp["FX1"]["instrType"] == "FX" and dp["FX1"]["mvRub"] == pytest.approx(1000 * USD)
    assert (dp["FX2"]["mvRub"], dp["FX2"]["fxDelta"]) == (0.0, {"USD": -400.0})
    assert desk["history"][-1]["dv01"] == pytest.approx(sum(p["fact"]["dv01"] for p in got.values() if p["fact"]))
    detail = c.get(f"/api/v1/runs/{rid}").json()
    assert detail["lineage"]["positions"]["sources"]["CORP"]["report_date"] == "2026-10-16"
    assert detail["coverage"]["_total"]["reported_value"] == 3 and detail["coverage"]["_total"]["not_valued"] == 2
    assert c.get(f"/api/v1/runs/{rid}/risk").status_code == 200
    with store.connect(store_url) as conn:  # T2: an FX forward is assumed but has no bond to reprice
        con = conn.cursor(row_factory=dict_row)
        run_row = con.execute("SELECT run_id, business_date, cut_type, market_snapshot_id FROM run WHERE run_id = %s",
                              (rid,)).fetchone()
        prow = con.execute("SELECT position_id, book_id, secid, quantity, quality, pv_rub, fact FROM run_position"
                           " WHERE run_id = %s AND fact IS NOT NULL ORDER BY ord", (rid,)).fetchall()
        ref = {r["kind"]: r["data"] for r in con.execute("SELECT kind, data FROM run_reference WHERE run_id = %s",
                                                         (rid,))}
    t2 = {p["position_id"]: p for p in reprice_run(SnapshotDir(FIX), run_row, prow, ref, {"curves": {"RUB RUONIA": 1}})
          ["positions"]}
    assert t2["FX2"]["code"] == "assumed" and t2["FX2"]["exact"] is None and t2["L1"]["exact"] is not None
    assert c.get(f"/api/v1/runs/{rid}/aggregates?dims=quality,ccy").status_code == 200
    h = c.get("/api/v1/bonds/history?instrument_id=SU26238RMFS4&curve=own").json()  # the Bonds tab: no 404
    assert [p["date"] for p in h["series"]] == [DATE] and h["series"][0]["face_amount"] == pytest.approx(10_000.0)
    assert h["series"][0]["clean_price"] == got["L1"]["fact"]["price"] and h["isin"] == got["L1"]["fact"]["isin"]
    assert c.get("/api/v1/bonds/history?isin=XS999&from=2026-01-01").json()["series"] == []
    assert c.get("/api/v1/bonds/history").status_code == 422

    js = c.get("/desk-data.js").text
    desk = json.loads(js[len("window.DESK = "):-2])
    pos = {p["id"]: p for p in desk["positions"]}
    assert set(pos) == {k for k, *_ in rows}  # B4 and O1 are not valued: listed after the rest, without PV
    assert [k for k in pos if pos[k].get("notValued")] == ["B4", "O1"] and pos["B4"]["mvRub"] is None
    assert (pos["FX1"]["instrType"], pos["FX1"]["ccy"], pos["FX1"]["fxDelta"]) == ("FX", "USD",
                                                                                  {"USD": pytest.approx(1000.0)})
    assert pos["B2"]["name"] == "Bond abroad (из отчёта)" and pos["B2"]["instrumentId"] == "XS000"
    assert set(desk["BOOKS"]) == {"OFZ", "CORP", "TSY"} and desk["BOOKS"]["TSY"]["bookType"] == "DERIVS"


def test_book_with_only_unvalued_positions_is_in_the_reference(ctx, store_url):
    """A derivatives book whose rows all lack mv_rub is still a book of the run: the Desk Monitor shows only the
    positions of window.DESK.BOOKS, so without it the book's rows would vanish from coverage."""
    inp = inputs([("L1", "OFZ", "SU26238RMFS4", 10.0), ("O1", "DERONLY", None, None)],
                 {"O1": {"asset_class": "otc", "source_instrument_id": "IRS-1", "ccy": "RUB"}})
    _, positions = run(ctx, inp)
    with store.connect(store_url) as con:
        rid, status, _ = desk_run.publish(con, ctx, positions, prev=None, inputs=inp)
    assert status == "complete"
    desk = TestClient(create_app(store_url)).get(f"/api/v1/compat/desk?run_id={rid}").json()
    assert set(desk["BOOKS"]) == {"OFZ", "DERONLY"}
    assert [p["id"] for p in desk["positions"] if p["book"] == "DERONLY" and p.get("notValued")] == ["O1"]


def test_books_from_the_store_and_risk_fields_round_trip(ctx, store_url):
    """The run's book reference is the store's book_rev (name, desk, entity, book_type), not the demo BOOKS; the
    demo books keep theirs. The new v1 fields survive the store (position_input) into the valuation."""
    d = date.fromisoformat(DATE)
    with store.connect(store_url) as con:
        store.store_books(con, [
            {"book_id": "RATES1", "name": "Ставки 1", "desk": "Rates", "book_type": "TRADING", "entity": "ENT-A",
             "active": True},
            {"book_id": "DER1", "name": "Деривативы 1", "desk": "Derivs", "book_type": "DERIVS", "entity": "ENT-B",
             "active": True, "trader": "T1"}], "fp")
        con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, 'p.csv', 'p.csv', 1,"
                    " now())", ("a" * 64,))
        store.store_inbox_set(
            con, book_id="DER1", valid_date=d, source="positions_csv", priority=None, report_date=d, report_ts=None,
            seq=None, generated_at=None, file_mtime=datetime.now(UTC), file_name="p.csv", sha256="a" * 64,
            adapter_fingerprint="fp", unit_hash="u", rows=[
                {"_src": "L2", "book_id": "DER1", "position_id": "DER1|IRS-7", "asset_class": "otc",
                 "source_instrument_id": "IRS-7", "ccy": "RUB", "mv_rub": "125000.5", "maturity_date": "2031-01-10",
                 "risk_dv01_rub": "-850.25", "risk_cs01_rub": "-12"},
                {"_src": "L3", "book_id": "DER1", "position_id": "DER1|IRS-8", "asset_class": "otc",
                 "source_instrument_id": "IRS-8", "source_instrument_name": "IRS 8", "ccy": "USD",
                 "maturity_date": "2029-03-01", "x_notional": "1000000", "x_kind": "IRS"}])
        store.load_positions(con, [("L1", "RATES1", "SU26238RMFS4", 10.0), ("L2", "OFZ", "SU26238RMFS4", 5.0)],
                             valid_date=d, source_name="tests", source_sha256="-")
        inp = store.positions_for(con, d)
        assert set(inp.books) == {"RATES1", "DER1"} and inp.books["DER1"]["entity"] == "ENT-B"
        x = inp.extra["DER1|IRS-7"]
        assert (x["maturity_date"], x["risk_dv01_rub"], x["risk_cs01_rub"]) == (date(2031, 1, 10), -850.25, -12.0)
        _, positions = run(ctx, inp)
        rid, status, _ = desk_run.publish(con, ctx, positions, prev=None, inputs=inp)
        books = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'books'", (rid,)).fetchone()[0]
    assert status == "complete"
    by = {b["book_id"]: b for b in books}
    assert (by["DER1"]["name"], by["DER1"]["desk"], by["DER1"]["entity"], by["DER1"]["book_type"],
            by["DER1"]["trader"]) == ("Деривативы 1", "Derivs", "ENT-B", "DERIVS", "T1")
    assert (by["RATES1"]["entity"], by["OFZ"]["entity"], by["OFZ"]["name"]) == ("ENT-A", "Демо", "ОФЗ (демо)")
    desk = TestClient(create_app(store_url)).get(f"/api/v1/compat/desk?run_id={rid}").json()
    assert {b["entity"] for b in desk["BOOKS"].values()} == {"ENT-A", "ENT-B", "Демо"}
    pos = {p["id"]: p for p in desk["positions"]}
    assert pos["DER1|IRS-7"]["dv01"] == -850.25 and pos["DER1|IRS-7"]["entity"] == "ENT-B"
    nv = pos["DER1|IRS-8"]  # only x_* fields, as an adapter sends a derivative whose units are unconfirmed
    assert nv["notValued"] and nv["entity"] == "ENT-B" and nv["mvRub"] is None and nv["dv01"] == 0
    assert (nv["name"], nv["ccy"], nv["maturity"], nv["segment"], nv["assetClass"]) == (
        "IRS 8", "USD", "2029-03-01", "Деривативы", "OTC-деривативы")
    assert nv["extra"]["reported"]["x"] == {"x_notional": "1000000", "x_kind": "IRS"} and nv["extra"]["reported"]["src"] == "L3"


def test_book_rows_fallbacks():
    rows = tdm_run.book_rows(["OFZ", "REAL", "META"], {"META": {"book_id": "META", "name": "M", "desk": "D",
                                                                 "book_type": "HTM", "entity": "E"}})
    assert [r["book_id"] for r in rows] == ["OFZ", "REAL", "META"]
    assert rows[0]["entity"] == "Демо" and (rows[1]["name"], rows[1]["entity"]) == ("REAL", "—")
    assert (rows[2]["name"], rows[2]["desk"], rows[2]["entity"], rows[2]["book_type"]) == ("M", "D", "E", "HTM")


def test_portfolio_of_every_position(ctx, store_url):
    """The report's Portfolio column (x_portfolio) reaches window.DESK on every position, valued or not; a legacy
    row has none. The Desk Monitor filters by it (HTM vs trading)."""
    d = date.fromisoformat(DATE)
    with store.connect(store_url) as con:
        con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, 'p.csv', 'p.csv', 1,"
                    " now())", ("b" * 64,))
        store.store_inbox_set(
            con, book_id="MIX1", valid_date=d, source="positions_csv", priority=None, report_date=d, report_ts=None,
            seq=None, generated_at=None, file_mtime=datetime.now(UTC), file_name="p.csv", sha256="b" * 64,
            adapter_fingerprint="fp", unit_hash="u", rows=[
                {"_src": "L2", "book_id": "MIX1", "position_id": "MIX1|OFZ", "asset_class": "bond",
                 "instrument_id": "SU26238RMFS4", "quantity": "10", "ccy": "RUB", "x_portfolio": "HTM"},
                {"_src": "L3", "book_id": "MIX1", "position_id": "MIX1|IRS", "asset_class": "otc",
                 "source_instrument_id": "IRS-9", "ccy": "RUB", "x_portfolio": "Торговый"}])
        store.load_positions(con, [("L1", "OFZ", "SU26238RMFS4", 5.0)], valid_date=d, source_name="tests",
                             source_sha256="-")
        inp = store.positions_for(con, d)
        _, positions = run(ctx, inp)
        rid, status, _ = desk_run.publish(con, ctx, positions, prev=None, inputs=inp)
    assert status == "complete"
    desk = TestClient(create_app(store_url)).get(f"/api/v1/compat/desk?run_id={rid}").json()
    pos = {p["id"]: p for p in desk["positions"]}
    assert not pos["MIX1|OFZ"].get("notValued") and pos["MIX1|IRS"]["notValued"]
    assert (pos["MIX1|OFZ"]["portfolio"], pos["MIX1|IRS"]["portfolio"], pos["L1"]["portfolio"]) == (
        "HTM", "Торговый", None)
