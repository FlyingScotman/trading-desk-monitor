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
USD = 83.9497  # CBR on the fixture date
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
    assert o.valuation.quality == "assumed" and o.valuation.pv_rub == pytest.approx(-1000 * USD)
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
    p, out = run(ctx, inputs([("S", "FX", None, None), ("F", "FX", None, None), ("R", "FX", None, None)], {
        "S": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 1000.0},
        "F": {"asset_class": "fx", "ccy": "USD", "amount_ccy": -1000.0, "value_date": date(2027, 1, 15)},
        "R": {"asset_class": "fx", "ccy": "RUB", "amount_ccy": 5e6, "value_date": date(2026, 10, 16)}}))
    assert not p.requests
    s, f, r = (o.valuation for _, _, o in out)
    assert (s.quality, s.pv_rub, s.dv01_rub) == ("market_value", pytest.approx(1000 * USD), 0.0)
    assert f.quality == "assumed" and f.pv_rub == pytest.approx(-1000 * USD) and "not discounted" in f.assumptions[-1]
    assert (r.quality, r.pv_rub) == ("market_value", 5e6)


def test_otc_not_valued(ctx):
    o = one(ctx, {"asset_class": "otc", "source_instrument_id": "IRS-1"})
    assert o.valuation is None and o.reason == "asset_class_not_supported_s7a"


def test_publish_api_and_compat(ctx, store_url, tmp_path):
    rows = [("L1", "OFZ", "SU26238RMFS4", 10.0), ("B1", "CORP", None, None), ("B2", "CORP", None, 10.0),
            ("B3", "CORP", None, None), ("B4", "CORP", None, None), ("FX1", "TSY", None, None),
            ("FX2", "CORP", None, None), ("FX3", "CORP", None, None), ("O1", "CORP", None, None)]
    inp = inputs(rows, {
        "B1": bond(source_instrument_id="RU000A109XR1", isin=FLOATER, face_outstanding_ccy=8_000.0,
                   mark_price_pct=99.0, mv_rub=8000.0, src="R12", x={"x_desk": "a"}),
        "B2": bond(source_instrument_id="XS000", source_instrument_name="Bond abroad", mv_rub=1e6),
        "B3": bond(source_instrument_id="XS001", ccy="USD", mark_price_pct=95.0, face_amount_ccy=1e5),
        "B4": bond(source_instrument_id="XS002"),
        "FX1": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 1000.0},
        "FX2": {"asset_class": "fx", "ccy": "USD", "amount_ccy": -400.0, "value_date": date(2027, 1, 15)},
        "FX3": {"asset_class": "fx", "ccy": "RUB", "amount_ccy": 5e6},
        "O1": {"asset_class": "otc", "source_instrument_id": "IRS-1"}})
    _, positions = run(ctx, inp)
    with store.connect(store_url) as con:
        rid, status, warnings = desk_run.publish(con, ctx, positions, prev=None, inputs=inp)
    assert status == "complete" and any("B4" in w and "unmapped_instrument" in w for w in warnings)
    tdm_run.publish(str(tmp_path / "tdm.db"), ctx, positions, prev=None, inputs=inp)  # the tdm path takes them too

    c = TestClient(create_app(store_url))
    got = {p["position_id"]: p for p in c.get(f"/api/v1/runs/{rid}/positions").json()["positions"]}
    assert [got[k]["quality"] for k, *_ in rows] == ["exact", "assumed", "reported_value", "reported_value", None,
                                                     "market_value", "assumed", "market_value", None]
    assert (got["B1"]["secid"], got["B1"]["quantity"]) == (FLOATER, pytest.approx(10.0))
    b1 = got["B1"]["fact"]["extra"]
    assert b1["reported"] == {"asset_class": "bond", "ccy": "RUB", "source_instrument_id": FLOATER, "isin": FLOATER,
                              "face_outstanding_ccy": 8000.0, "mark_price_pct": 99.0, "mv_rub": 8000.0, "src": "R12",
                              "x": {"x_desk": "a"}}
    assert got["FX1"]["secid"] is None and got["FX1"]["fact"]["fx_delta"] == {"USD": pytest.approx(1000.0)}
    assert got["FX3"]["fact"].get("fx_delta") is None and got["FX2"]["fact"]["maturity"] == "2027-01-15"
    assert got["B2"]["fact"]["name"] == "Bond abroad" and got["B2"]["dv01_rub"] == 0.0
    assert got["O1"]["reason"] == "asset_class_not_supported_s7a: otc" and got["O1"]["fact"] is None
    detail = c.get(f"/api/v1/runs/{rid}").json()
    assert detail["lineage"]["positions"]["sources"]["CORP"]["report_date"] == "2026-10-16"
    assert detail["coverage"]["_total"]["reported_value"] == 2 and detail["coverage"]["_total"]["not_valued"] == 2
    assert c.get(f"/api/v1/runs/{rid}/risk").status_code == 200
    with store.connect(store_url) as conn:  # T2: an FX forward is assumed but has no bond to reprice
        con = conn.cursor(row_factory=dict_row)
        run_row = con.execute("SELECT run_id, business_date, cut_type, market_snapshot_id FROM run WHERE run_id = %s",
                              (rid,)).fetchone()
        prow = con.execute("SELECT position_id, book_id, secid, quantity, quality, pv_rub, fact FROM run_position"
                           " WHERE run_id = %s AND fact IS NOT NULL ORDER BY ord", (rid,)).fetchall()
        ref = {r["kind"]: r["data"] for r in con.execute("SELECT kind, data FROM run_reference WHERE run_id = %s",
                                                         (rid,))}
    t2 = {p["position_id"]: p for p in reprice_run(SnapshotDir(FIX), run_row, prow, ref, {"curves": {"RUB OFZ": 1}})
          ["positions"]}
    assert t2["FX2"]["code"] == "assumed" and t2["FX2"]["exact"] is None and t2["L1"]["exact"] is not None
    assert c.get(f"/api/v1/runs/{rid}/aggregates?dims=quality,ccy").status_code == 200

    js = c.get("/desk-data.js").text
    desk = json.loads(js[len("window.DESK = "):-2])
    pos = {p["id"]: p for p in desk["positions"]}
    assert set(pos) == {k for k, *_ in rows} - {"B4", "O1"}
    assert (pos["FX1"]["instrType"], pos["FX1"]["ccy"], pos["FX1"]["fxDelta"]) == ("FX", "USD",
                                                                                  {"USD": pytest.approx(1000.0)})
    assert pos["B2"]["name"] == "Bond abroad" and pos["B2"]["instrumentId"] == "XS000"
    assert set(desk["BOOKS"]) == {"OFZ", "CORP", "TSY"} and desk["BOOKS"]["TSY"]["bookType"] == "DERIVS"
