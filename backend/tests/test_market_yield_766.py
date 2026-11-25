"""Recalculate flagged quotes with the stored hub contract (TDM#766)."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_market_history import FIX, STATIC, shifted, write

from desk import store
from desk.api import concepts, market_history
from desk.marketdata import history, market_analytics, static_facts
from desk.marketdata.bundle import read_bundle

BEFORE, PREV, DAY = date(2026, 9, 28), date(2026, 10, 1), date(2026, 10, 2)
CROSSING = date(2026, 9, 29)
FX, RUB, FLOATER, NO_PRICE, NO_TERMS, ORDINARY, LEGACY = (
    "SYN-FX", "SYN-RUB", "SYN-FLOAT", "SYN-NO-PRICE", "SYN-NO-TERMS", "SYN-ORDINARY", "SYN-LEGACY",
)
RAW = {"yield_pct": 53.94, "duration_days": 9999.0, "gspread_bp": 8765.0, "zspread_bp": 7654.0}
RUB_YIELD = (1100 / (1000 + 400 / 365) - 1) * 100
CPN_DATES = [
    date(y, m, 30 if m == 9 else 31)
    for y, m in ((2026, 9), (2027, 3), (2027, 9), (2028, 3), (2028, 9), (2029, 3), (2029, 9), (2030, 3))
]


def _instrument(secid, *, ccy="RUB", face="1000", maturity="2027-10-05", cpn_type="fixed", flagged=True, manual=False):
    return {
        "secid": secid, "name": secid, "status": "active", "bond_class": "corporate", "coupon_type": cpn_type,
        "face_unit": ccy, "currency_id": ccy, "face_value": face, "maturity_date": maturity, "day_count": "30E/360",
        "calendar": "MOEX_STOCK", "labels": ["redemptions_manual_override"] if manual else [],
        "source_analytics_unreliable_reasons": ["redemptions_rounded_by_source"] if flagged else [],
    }


def _static(tmp_path):
    original = read_bundle(FIX / f"{STATIC}.json.gz")
    instruments = [
        _instrument(FX, ccy="USD", face="0.035", maturity=CPN_DATES[-1].isoformat(), manual=True),
        _instrument(RUB), _instrument(FLOATER, cpn_type="floating"), _instrument(NO_PRICE),
        _instrument(ORDINARY, flagged=False), _instrument(LEGACY),
    ]
    instruments[-1]["labels"] = ["future_redemptions_differ_from_face"]
    instruments[-1]["source_analytics_unreliable_reasons"] = []
    coupons = [
        {"secid": FX, "coupon_no": k, "start_date": start.isoformat(), "pay_date": end.isoformat(),
         "rate_pct": "7.5", "amount_per_bond": "0", "status": "known"}
        for k, (start, end) in enumerate(zip([date(2026, 3, 31), *CPN_DATES[:-1]], CPN_DATES, strict=True), 1)
    ]
    coupons += [
        {"secid": sid, "coupon_no": 1, "start_date": "2026-10-01", "pay_date": "2027-10-05",
         "rate_pct": "10", "amount_per_bond": "100", "status": "known"}
        for sid in (RUB, NO_PRICE)
    ]
    redemptions = [
        {"secid": FX, "date": d.isoformat(), "kind": "maturity" if d == CPN_DATES[-1] else "amortization",
         "value_per_bond": "0.005", "source": "manual_override" if d != CPN_DATES[0] else "face_steps"}
        for d in CPN_DATES
    ]
    redemptions += [{"secid": sid, "date": "2027-10-05", "kind": "maturity", "value_per_bond": "1000"}
                    for sid in (RUB, NO_PRICE)]
    data = {"instruments": instruments, "coupon_periods": coupons, "redemptions": redemptions, "offers": [],
            "issuers": [], "floater_formulas": [], "calendars": original.data["calendars"]}
    sid = write(tmp_path, {**original.doc, "business_date": "2026-09-30", "schema_version": "market_snapshot/1.10",
                           "data": data})
    path = tmp_path / f"{sid}.json.gz"
    return read_bundle(path), path


def _prices(con):
    for day, p, face, currency, accrued in (
        (BEFORE, 100, 0.04, "USD", float(Decimal("0.04") * Decimal("0.075") * 178 / 360)),
        (CROSSING, 100, 0.035, "USD", 0.0),  # quoted accrued is too coarsely rounded for a trade-date adjustment
        (PREV, 101, 0.035, "SUR", 0.01),
        (DAY, 100, 0.035, "SUR", 0.02),
    ):
        con.execute("""INSERT INTO md_hub_bond
                         (trade_date, secid, board, clean_pct, yield_pct, yield_close_pct, yield_date, duration_days,
                          gspread_bp, zspread_bp, accrued_per_bond, face_value, currency_id, value, fetch_id)
                       VALUES (%s, %s, 'TQCB', %s, %s, 45.67, '2027-01-01', %s, %s, %s, %s, %s, %s, 1000, 1)""",
                    (day, FX, p, *RAW.values(), accrued, face, currency))
    for sid in (RUB, FLOATER, NO_PRICE, NO_TERMS, ORDINARY):
        for d in (PREV, DAY):
            con.execute("""INSERT INTO md_hub_bond
                             (trade_date, secid, board, clean_pct, yield_pct, yield_close_pct, yield_date, duration_days,
                              gspread_bp, zspread_bp, accrued_per_bond, face_value, currency_id, value, fetch_id)
                           VALUES (%s, %s, 'TQCB', %s, %s, 45.67, '2027-01-01', %s, %s, %s, %s,
                                   1000, 'SUR', 1000, 1)""",
                        (d, sid, None if sid == NO_PRICE else 100, *RAW.values(), (d - PREV).days * 100 / 365))
    params = {**{k: "0" for k in ("B2", "B3", *(f"G{i}" for i in range(1, 10)))},
              "B1": str(math.log1p(0.12) * 10_000), "T1": "1"}
    con.execute("INSERT INTO md_kbd (business_date, snapshot_id, known_at) VALUES (%s, 'legacy', now())", (BEFORE,))
    con.execute("INSERT INTO md_hub_zcyc (trade_date, params, fetch_id) VALUES (%s, %s, 1)", (DAY, Jsonb(params)))
    con.execute("INSERT INTO md_bond_eod (business_date, secid, board, clean_pct, yield_pct, duration_days)"
                " VALUES (%s, %s, 'TQCB', 100, 53.94, 9999)", (BEFORE, LEGACY))


def test_market_core_after_upgrade(pg_server, tmp_path, monkeypatch):
    pg_server.psql("CREATE DATABASE market_yield_766;")
    url = pg_server.get_uri("market_yield_766")
    old_migrations = tmp_path / "migrations"
    old_migrations.mkdir()
    for path in store.MIGRATIONS.glob("*.sql"):
        if path.name.startswith(("000", "001", "002")):
            (old_migrations / path.name).write_bytes(path.read_bytes())
    with monkeypatch.context() as m:
        m.setattr(store, "MIGRATIONS", old_migrations)
        store.migrate(url)
    static, path = _static(tmp_path)
    with store.connect(url) as con:
        assert history.load_snapshot(con, static) == "loaded"
        con.execute("INSERT INTO md_static_summary (snapshot_id, business_date, version, data)"
                    " VALUES (%s, %s, 5, %s)", (static.id, static.business_date, Jsonb({"calendar": {}})))
        for i in static.data["instruments"]:
            con.execute("INSERT INTO md_static_bond VALUES (%s, %s, %s)",
                        (static.id, i["secid"], Jsonb({"effective_yield_unreliable": bool(i["labels"] or
                                                           i["source_analytics_unreliable_reasons"])})))
        _prices(con)
        for day in (BEFORE, PREV):
            rid = con.execute(
                "INSERT INTO run (run_type, business_date, cut_type, market_snapshot_id, static_snapshot_id,"
                " private_inputs_hash, position_set_ids, positions_known_at, core_version, backend_version, lineage)"
                " VALUES ('EOD', %s, 'eod_final', 'legacy', %s, 'h', '{}', now(), 'c', 'b', '{}') RETURNING run_id",
                (day, static.id)).fetchone()[0]
            con.execute("INSERT INTO run_position (run_id, position_id, ord, book_id, secid, quantity, quality, fact)"
                        " VALUES (%s, 'P1', 0, 'B', %s, 1, 'market_value', %s)",
                        (rid, LEGACY, Jsonb({"instr_type": "BOND", "instrument_id": LEGACY, "ytm": 53.94})))
            con.execute("UPDATE run SET status = 'complete' WHERE run_id = %s", (rid,))
    assert store.migrate(url) == ["0030_md_bond_analytics_quality"]
    assert store.migrate(url) == []
    with store.connect(url) as con:
        # Snapshot flags must also suppress an overlapping row from the hub history.
        doc = shifted(DAY.isoformat(), 0)
        doc["schema_version"] = "market_snapshot/1.10"
        quote = doc["data"]["bond_quotes"][0]
        doc["data"]["bond_quotes"] = [
            dict(quote, secid=NO_TERMS, source_analytics_unreliable={"fields": ["duration_days"], "reasons": ["bad"]})
        ]
        sid = write(tmp_path, doc)
        assert history.load_snapshot(con, read_bundle(tmp_path / f"{sid}.json.gz")) == "loaded"

    run = {"run_id": 766, "business_date": DAY}
    with store.connect(url) as con:
        con.row_factory = dict_row
        assert market_history.market(con, run)["bonds"][FX]["y"][0] is None
        masked = con.execute("SELECT * FROM md_bond_day WHERE business_date = %s AND secid = %s", (DAY, FX)).fetchone()
        assert all(masked[k] is None for k in (*RAW, "yield_date"))
    with store.connect(url) as con:
        loaded_at = con.execute("SELECT loaded_at FROM md_static_summary").fetchone()[0]
        assert static_facts.refresh_cached(con, tmp_path) == 1
        assert static_facts.refresh_cached(con, tmp_path) == 0
        assert con.execute("SELECT loaded_at FROM md_static_summary").fetchone()[0] == loaded_at
    path.unlink()  # none of the read paths may require the snapshot after load_static

    with store.connect(url) as con:
        con.row_factory = dict_row
        market = concepts.universe(con, DAY, set())
        fx = next(r for r in market["universeFx"] if r["id"] == FX)
        rub = {r["id"]: r for r in market["universeRub"]}
        assert 7 < fx["ytm"] < 8 and 1.5 < fx["dur"] < 2.2
        assert fx["ytmMoex"] is fx["spread"] is fx["zspread"] is None
        assert fx["yieldSource"] == "core" and fx["yieldManualOverride"] is True
        assert "ручная правка" in fx["yieldNote"] and "НКД рассчитан в валюте номинала" in fx["yieldNote"]
        assert fx["chgBp"] > 40
        assert rub[RUB]["ytm"] == pytest.approx(RUB_YIELD)
        assert rub[RUB]["dur"] == pytest.approx(1)
        assert rub[RUB]["spread"] == pytest.approx((RUB_YIELD - 12) * 100)
        assert rub[RUB]["zspread"] == pytest.approx((RUB_YIELD - 12) * 100)
        assert rub[ORDINARY]["ytm"] == rub[ORDINARY]["ytmMoex"] == 53.94
        assert "yieldNote" not in rub[ORDINARY]
        for secid, reason in ((FLOATER, "Флоатер"), (NO_PRICE, "Нет чистой цены"), (NO_TERMS, "Нет условий")):
            b = rub[secid]
            assert all(b[k] is None for k in ("ytm", "ytmMoex", "dur", "spread", "zspread", "tenor", "put"))
            assert reason in b["yieldNote"] and b["yieldSource"] is None
        movers = {r["name"]: r for r in market["marketMovers"]}
        assert movers[RUB]["ytm"] == pytest.approx(RUB_YIELD) and movers[RUB]["yieldSource"] == "core"
        assert movers[NO_PRICE]["ytm"] is None and movers[NO_PRICE]["chgBp"] is None
        changes = market_history.market(con, run)["bonds"]
        assert changes[FX]["y"][0] == round(fx["chgBp"], 1) and changes[FX]["p"][0] == -1

        calls = []
        real_batch = market_analytics.bond_metrics_batch

        def batch(items):
            calls.append([i.settlement_date for i in items])
            return real_batch(items)

        monkeypatch.setattr(market_analytics, "bond_metrics_batch", batch)
        points = history.bond_history(con, instrument_id=FX, isin=None)["series"]
        assert calls == [[CROSSING, CPN_DATES[0], DAY, date(2026, 10, 5)]]  # Friday uses Monday T+1
        assert all(6 < p["ytm"] < 8 for p in points)
        assert points[0]["duration_days"] < points[1]["duration_days"]  # near-term amortization drops out at T+1
        assert points[-1]["ytm"] == pytest.approx(fx["ytm"]) and points[-1]["yield_date"] == CPN_DATES[-1]
        assert all(p["g_spread_bp"] is p["z_spread_bp"] is p["moex_zspread_bp"] is None for p in points)
        assert all(p["yield_source"] == "core" and p["yield_manual_override"] for p in points)
        # Independently reprice the dirty amount from the unrounded semiannual coupons and principal.
        # A denominated USD trade supplies published USD accrued plus one ACT/365 accrual day.
        paid = sum((0.005 + (0.04 - i * 0.005) * 0.075 / 2) /
                   (1 + points[0]["ytm"] / 100) ** ((d - date(2026, 9, 29)).days / 365)
                   for i, d in enumerate(CPN_DATES))
        assert paid == pytest.approx(0.04 + 0.04 * 0.075 * (178 / 360 + 1 / 365), abs=1e-11)
        # T+1 is the coupon and amortization date. The buyer pays 0.035 with zero accrued.
        crossing_pv = sum((0.005 + (0.04 - i * 0.005) * 0.075 / 2) /
                          (1 + points[1]["ytm"] / 100) ** ((d - CPN_DATES[0]).days / 365)
                          for i, d in enumerate(CPN_DATES[1:], 1))
        assert crossing_pv == pytest.approx(0.035, abs=1e-11)
        for ambiguous_face in (0.04, None):
            con.execute("UPDATE md_hub_bond SET face_value = %s WHERE trade_date = %s AND secid = %s",
                        (ambiguous_face, CROSSING, FX))
            point = history.bond_history(con, instrument_id=FX, isin=None, from_=CROSSING, to=CROSSING)["series"][0]
            assert all(point[k] is None for k in ("ytm", "duration_days", "yield_date", "g_spread_bp", "z_spread_bp"))
            assert point["yield_source"] is None
            assert "День перехода амортизации" in point["yield_note"] and "номинал котировки" in point["yield_note"]
            market = concepts.universe(con, CROSSING, set())
            quote = next(r for r in market["universeFx"] if r["id"] == FX)
            assert quote["ytm"] is quote["dur"] is quote["chgBp"] is None
        rub_points = history.bond_history(con, instrument_id=RUB, isin=None)["series"]
        assert rub_points[0]["ytm"] is not None and rub_points[0]["g_spread_bp"] is rub_points[0]["z_spread_bp"] is None
        assert rub_points[-1]["spread_to_curve_bp"] == pytest.approx((RUB_YIELD - 12) * 100)
        assert rub_points[-1]["z_spread_bp"] == pytest.approx((RUB_YIELD - 12) * 100)
        failed, run_only = history.bond_history(con, instrument_id=LEGACY, isin=None)["series"]
        assert failed["ytm"] is failed["yield_date"] is failed["duration_days"] is None
        assert "график погашений" in failed["yield_note"].lower()
        assert "Нет рыночной котировки" in run_only["yield_note"] and run_only.get("ytm") is None
