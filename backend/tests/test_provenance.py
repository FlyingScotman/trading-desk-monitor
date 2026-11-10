"""Where a position's data came from (TDM#617): ``GET /api/v1/positions/provenance``, the card's «Откуда данные». A run
written straight into the store with its sets: report rows with and without terms, terms without a report row (a
derivative off the report), a position the day's trades opened, one deal on two report rows, a valued and an unvalued
deal with a missing curve, an old run without lineage. Synthetic data only."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_deriv_e2e_555 import ctx, moved, prev, published  # noqa: F401 - the published run's fixtures

from desk import store
from desk.api import create_app

BD = date(2026, 10, 20)
SHA = {k: k * 64 for k in "abcde"}
LINEAGE_FX = {"spots": {"USD": "80 cbr, value 2026-10-21", "CNY": "11 cbr, value 2026-10-21"},
              "curves": {"USD FX": {"status": "ok"}, "EUR FX": {"status": "missing", "error": "no quotes"}},
              "index_fixings": {}, "fixings": []}
LINEAGE_RATES = {"status": {"ruonia_ois": "ok", "key_rate_swap": "ok", "fixings": "ok"},
                 "fixings": {"RUONIA": {"rows": 30, "latest": "2026-10-19",   # a day without a publication: bridged
                                        "bridged_days": [{"date": "2026-10-05", "from": "2026-10-02"}]},
                             "KEY_RATE": {"rows": 30}}}


def _file(con, sha: str, name: str) -> None:
    con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, %s, %s, 1, now())",
                (sha, "/synthetic/in/" + name, name))


def _dataset(con, ds: str, sha: str, name: str, rows: list[dict], *, source: str, valid: date = BD,
             scope: dict | None = None) -> int:
    scope = scope or {"book_id": "B1"}
    sid = con.execute(
        "INSERT INTO dataset_set (dataset, scope, scope_key, valid_date, revision, source, report_date,"
        " inbox_file_sha256, source_name, adapter_fingerprint, unit_hash, row_count, recorded_at)"
        " VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s, 'fp', 'uh', %s, now() - interval '1 hour') RETURNING set_id",
        (ds, Jsonb(scope), json.dumps(scope, sort_keys=True), valid, source, valid, sha, name, len(rows))).fetchone()[0]
    for n, r in enumerate(rows, 1):
        con.execute("INSERT INTO dataset_row (set_id, row_no, row_key, data) VALUES (%s, %s, %s, %s)",
                    (sid, n, str(n), Jsonb(r)))
    return sid


def _run(con, lineage: dict, set_ids: list[int], *, day: date = BD, version: str = "t") -> int:
    rid = con.execute(
        "INSERT INTO run (run_type, business_date, prev_date, cut_type, market_snapshot_id, static_snapshot_id,"
        " private_inputs_hash, position_set_ids, positions_known_at, core_version, backend_version, lineage)"
        " VALUES ('EOD', %s, %s, 'eod_final', 'synthetic-market', 'synthetic-static', 'h', %s, now(), 't', %s, %s)"
        " RETURNING run_id", (day, day - timedelta(days=1), set_ids, version, Jsonb(lineage))).fetchone()[0]
    con.execute("INSERT INTO run_reference VALUES (%s, 'curves', %s)", (rid, Jsonb([
        {"curve_id": "RUB OFZ", "name": "ОФЗ (КБД MOEX)"}, {"curve_id": "USD FX", "name": "USD: из своп-пойнтов"}])))
    return rid


def _pos(con, rid: int, pid: str, n: int, *, fact: dict | None = None, reason: str | None = None,
         pnl: dict | None = None) -> None:
    con.execute("INSERT INTO run_position (run_id, position_id, ord, book_id, secid, quantity, quality, reason, fact, pnl)"
                " VALUES (%s, %s, %s, 'B1', NULL, 1, %s, %s, %s, %s)",
                (rid, pid, n, (fact or {}).get("extra", {}).get("quality") if fact else None, reason,
                 Jsonb(fact) if fact else None, Jsonb(pnl) if pnl else None))


@pytest.fixture
def seeded(store_url):
    with store.connect(store_url) as con:
        for k, name in (("a", "positions_20261020.csv"), ("b", "terms_20261020.csv"), ("c", "trades_20261020.csv"),
                        ("d", "ocp_20261020.csv"), ("e", "trades_20261019.csv")):
            _file(con, SHA[k], name)
        ps = con.execute(
            "INSERT INTO position_set (book_id, valid_date, revision, source_name, source_file_sha256, source,"
            " report_date, inbox_file_sha256) VALUES ('B1', %s, 1, 'positions_20261020.csv', %s, 'positions_csv', %s,"
            " %s) RETURNING set_id", (BD, SHA["a"], BD, SHA["a"])).fetchone()[0]
        for pid, cls, ccy, deal, vd in (("FX1", "fx", "USD", "T1", "2026-11-20"), ("FX2", "fx", "USD", "T9", "2026-11-20"),
                                        ("FX3", "fx", "CNY", "T4", "2026-11-20"), ("CCS1", "otc", "CNY", "T3", None),
                                        ("D1", "otc", "RUB", "T5", None), ("D2", "otc", "RUB", "T5", None),
                                        ("BND", "bond", "RUB", None, None), ("CCS2", "otc", "CNY", "T6", None),
                                        ("FX4", "fx", "CNY", "T8", "2026-11-20"), ("NA1", None, "RUB", "Z1", None),
                                        ("NA2", None, "RUB", "T10", None), ("NDF1", "fx", "USD", "T11", "2026-11-20"),
                                        ("FUT1", "fx", "USD", None, "2026-12-15"), ("IRS1", "otc", "RUB", "T13", None),
                                        ("IRS2", "otc", "RUB", "T14", None), ("REPO1", "otc", "RUB", "T15", None),
                                        ("FXT", "fx", "USD", "T16", "2026-10-20")):
            con.execute("INSERT INTO position_input (set_id, position_id, secid, quantity, asset_class, ccy, deal_id,"
                        " value_date, src) VALUES (%s, %s, NULL, NULL, %s, %s, %s, %s, %s)",
                        (ps, pid, cls, ccy, deal, vd, f"row {pid}"))
        # an FX future of the report the run did not value by its ticker (no number of contracts)
        con.execute("INSERT INTO position_input (set_id, position_id, secid, quantity, asset_class, ccy, value_date, x)"
                    " VALUES (%s, 'FUT2', NULL, NULL, 'fx', 'USD', '2026-12-15', %s)",
                    (ps, Jsonb({"x_product_group": "fx_future"})))
        terms = [
            {"trade_id": "T1", "book_id": "B1", "product_kind": "fx_forward", "base_ccy": "USD", "quote_ccy": "RUB"},
            {"trade_id": "T3", "leg_id": "1", "book_id": "B1", "product_kind": "ccs", "ccy": "RUB", "index_code": "RUONIA"},
            {"trade_id": "T3", "leg_id": "2", "book_id": "B1", "product_kind": "ccs", "ccy": "CNY", "fixed_rate_pct": "3"},
            {"trade_id": "T4", "book_id": "B1", "product_kind": "fx_forward", "base_ccy": "CNY", "quote_ccy": "RUB"},
            {"trade_id": "T6", "leg_id": "1", "book_id": "B1", "product_kind": "ccs", "ccy": "RUB", "index_code": "RUONIA"},
            {"trade_id": "T6", "leg_id": "2", "book_id": "B1", "product_kind": "ccs", "ccy": "CNY", "fixed_rate_pct": "2"},
            {"trade_id": "T8", "book_id": "B1", "product_kind": "fx_forward", "base_ccy": "CNY", "quote_ccy": "RUB"},
            {"trade_id": "T10", "book_id": "B1", "product_kind": "ois", "ccy": "RUB", "fixed_rate_pct": "9"},
            {"trade_id": "T11", "book_id": "B1", "product_kind": "fx_forward", "base_ccy": "USD", "quote_ccy": "RUB",
             "ndf": True, "fixing_date": BD.isoformat()},
            {"trade_id": "T1", "book_id": "B2", "product_kind": "fx_forward", "base_ccy": "EUR", "quote_ccy": "RUB"},
            # one trade, a leg per position: each position takes only its own leg
            {"trade_id": "T12", "leg_id": "a", "position_id": "FXA", "book_id": "B1", "product_kind": "fx_swap",
             "base_ccy": "USD", "quote_ccy": "RUB"},
            {"trade_id": "T12", "leg_id": "b", "position_id": "FXB", "book_id": "B1", "product_kind": "fx_swap",
             "base_ccy": "USD", "quote_ccy": "RUB"},
            # floating legs not started yet (no fixing needed) and started (fixings needed); a REPO on RUSFAR
            {"trade_id": "T13", "leg_id": "fix", "book_id": "B1", "product_kind": "irs", "ccy": "RUB",
             "fixed_rate_pct": "9", "start_date": "2026-12-01"},
            {"trade_id": "T13", "leg_id": "flt", "book_id": "B1", "product_kind": "irs", "ccy": "RUB",
             "index_code": "RUONIA", "start_date": "2026-12-01"},
            {"trade_id": "T14", "leg_id": "flt", "book_id": "B1", "product_kind": "irs", "ccy": "RUB",
             "index_code": "KEY_RATE", "start_date": "2026-01-15"},
            {"trade_id": "T15", "book_id": "B1", "product_kind": "repo", "ccy": "RUB", "index_code": "RUSFAR",
             "start_date": "2026-10-01"},
            # an FX row settling today (a spot by its date) whose terms the run gave core
            {"trade_id": "T16", "book_id": "B1", "product_kind": "fx_forward", "base_ccy": "USD", "quote_ccy": "RUB"},
            {"trade_id": "T5", "book_id": "B1", "product_kind": "irs", "ccy": "RUB", "fixed_rate_pct": "10"},
            {"trade_id": "T7", "book_id": "B1", "product_kind": "fx_forward", "base_ccy": "USD", "quote_ccy": "RUB"},
        ]
        ts = _dataset(con, "trade_terms", SHA["b"], "terms_20261020.csv", terms, source="terms_csv")
        tr = _dataset(con, "trades", SHA["c"], "trades_20261020.csv", [
            {"trade_id": "tr-1", "trade_version": 1, "status": "live", "book_id": "B1", "position_id": "N1",
             "asset_class": "bond", "side": "buy", "trade_date": BD.isoformat(), "quantity": "5"},
            # a newer version than the run took in: not «в позициях»
            {"trade_id": "tr-2", "trade_version": 3, "status": "live", "book_id": "B1", "position_id": "N1",
             "asset_class": "bond", "side": "sell", "trade_date": BD.isoformat(), "quantity": "1"},
            # a trade amended into another book: its current version is not this position's
            {"trade_id": "tr-3", "trade_version": 1, "status": "live", "book_id": "B1", "position_id": "N1",
             "asset_class": "bond", "side": "buy", "trade_date": BD.isoformat(), "quantity": "1"},
            {"trade_id": "tr-3", "trade_version": 2, "status": "live", "book_id": "B2", "position_id": "N1",
             "asset_class": "bond", "side": "buy", "trade_date": BD.isoformat(), "quantity": "1"},
            # a trade moved to another position of the book: its current version is not this position's
            {"trade_id": "tr-4", "trade_version": 1, "status": "live", "book_id": "B1", "position_id": "N1",
             "asset_class": "bond", "side": "buy", "trade_date": BD.isoformat(), "quantity": "1"},
            {"trade_id": "tr-4", "trade_version": 2, "status": "live", "book_id": "B1", "position_id": "N2",
             "asset_class": "bond", "side": "buy", "trade_date": BD.isoformat(), "quantity": "1"},
            # the same number in another book: not this position's
            {"trade_id": "T1", "trade_version": 1, "status": "live", "book_id": "B2", "asset_class": "fx",
             "side": "buy", "trade_date": BD.isoformat()}], source="trades_csv")
        # a trade of the day before: the start of the day of a book whose report is older
        tr0 = _dataset(con, "trades", SHA["e"], "trades_20261019.csv", [
            {"trade_id": "tr-0", "trade_version": 1, "status": "live", "book_id": "B1", "position_id": "N1",
             "asset_class": "bond", "side": "buy", "trade_date": "2026-10-19", "quantity": "2"}],
            source="trades_csv", valid=date(2026, 10, 19))
        _dataset(con, "ocp", SHA["d"], "ocp_20261020.csv", [{"book_id": "B1", "entity": "E1", "ccy": "USD"}],
                 source="ocp_csv", scope={"entity": "E1", "book_id": "B1"})
        lineage = {"positions": {"set_ids": [ps], "trade_terms": [{"scope": {"book_id": "B1"}, "set_id": ts}],
                                 "off_report": {"list": [{"book": "B1", "trade_id": "T7", "state": "included"}]}},
                   "rates": LINEAGE_RATES, "fx": LINEAGE_FX, "stale_datasets": {},
                   "intraday_trades": {"set_ids": [tr0, tr], "versions": {"tr-1": 1, "tr-2": 2}, "skipped_ids": {}}}
        rid = _run(con, lineage, [ps])
        core = {"rub_curve": "RUB RUONIA", "foreign_curves": ["USD FX"], "trades": ["T1"]}
        _pos(con, rid, "FX1", 1, fact={"ccy": "USD", "fx_delta": {"USD": 1.0},
                                       "extra": {"quality": "exact", "core": core, "fx_rates": {"USD": 80.0}}})
        _pos(con, rid, "FX2", 2, fact={"ccy": "USD", "extra": {"quality": "assumed",
                                                               "not_exact_reason": "no_trade_terms: synthetic"}})
        _pos(con, rid, "FX3", 3, fact={"ccy": "CNY", "extra": {"quality": "assumed", "core": {
            "rub_curve": "RUB RUONIA", "foreign_curves": ["CNY XCCY", "USD FX"], "trades": ["T4"],
            "curve_fallback_from": ["CNY FX"]}, "fx_rates": {"CNY": 11.0}}})
        _pos(con, rid, "CCS1", 4, reason="no_foreign_curve: no CNY curve in the run")
        _pos(con, rid, "D1", 5, reason="terms_ambiguous: trade T5")
        _pos(con, rid, "D2", 6, reason="terms_ambiguous: trade T5")
        _pos(con, rid, "BND", 7, fact={"ccy": "RUB", "instr_type": "BOND", "risk": {"RUB OFZ": [1.0]},
                                       "extra": {"quality": "exact", "price_basis": "close",
                                                 "reported": {"asset_class": "bond", "x": {"trades_report_gap": 2.0}}}})
        _pos(con, rid, "B1|T7", 8, fact={"ccy": "USD", "extra": {"quality": "exact", "off_report": True, "core": {
            "rub_curve": "RUB RUONIA", "foreign_curves": ["USD FX"], "trades": ["T7"]}}})
        _pos(con, rid, "N1", 9, fact={"ccy": "RUB", "instr_type": "BOND", "risk": {"RUB OFZ": [1.0]},
                                      "extra": {"quality": "stale_price"}},
             pnl={"extra": {"trades": [{"trade_id": "tr-1", "pnl_rub": 0.0}]}})
        # core did not value these: the value they got instead (the report's, the FX forward's 0) is no valuation
        _pos(con, rid, "CCS2", 10, fact={"ccy": "CNY", "extra": {"quality": "reported_value"}},
             reason="terms_invalid: synthetic")
        _pos(con, rid, "FX4", 11, reason="no_foreign_curve: no CNY curve in the run")
        _pos(con, rid, "NA1", 12, fact={"ccy": "RUB", "instr_type": "BOND", "risk": {"RUB OFZ": [1.0]},
                                        "extra": {"quality": "exact"}})
        _pos(con, rid, "NA2", 13, reason="no_rub_curve: synthetic")
        _pos(con, rid, "NDF1", 14, reason="no_fx_calendar: synthetic")
        # a CCS valued on the FX-implied CNY curve: the XCCY curve it wants first was not in the market
        _pos(con, rid, "CCS3", 15, fact={"ccy": "CNY", "extra": {"quality": "assumed", "core": {
            "product": "ccs", "rub_curve": "RUB RUONIA", "curves": ["RUB RUONIA", "CNY FX"], "trades": ["T3"]}}})
        _pos(con, rid, "FXA", 16, fact={"ccy": "USD", "extra": {"quality": "exact", "core": {
            "product": "fx_swap", "rub_curve": "RUB RUONIA", "foreign_curves": ["USD FX"], "trades": ["T12"]}}})
        _pos(con, rid, "FUT1", 17, fact={"ccy": "USD", "extra": {"quality": "exact", "core": {
            "product": "fx_future", "rub_curve": "RUB RUONIA", "foreign_curves": ["USD FX"], "trades": None}}})
        _pos(con, rid, "IRS1", 18, reason="no_rub_curve: synthetic")
        _pos(con, rid, "IRS2", 19, reason="no_rub_curve: synthetic")
        _pos(con, rid, "REPO1", 20, reason="no_fixings: REPO T15 on RUSFAR")
        _pos(con, rid, "FXT", 21, fact={"ccy": "USD", "extra": {"quality": "reported_value"}},
             reason="core_invalid_terms: synthetic")
        _pos(con, rid, "FUT2", 22, reason="no_quantity: future SYNF: no number of contracts")
        con.execute("UPDATE run SET status = 'complete', completed_at = now() WHERE run_id = %s", (rid,))
        old = _run(con, {}, [ps])   # an old run: no lineage at all
        _pos(con, old, "FX2", 1, reason="no_reported_value")
        con.execute("UPDATE run SET status = 'complete', completed_at = now() WHERE run_id = %s", (old,))
    return TestClient(create_app(store_url)), rid, old


def _get(c, rid, pid):
    r = c.get("/api/v1/positions/provenance", params={"id": pid, "run_id": rid})
    assert r.status_code == 200, r.text
    j = r.json()
    return j, {s["key"]: s for s in j["sources"]}, [m["code"] for m in j["mismatches"]]


def _market(j):
    return {(i["kind"], i["id"]): i for i in j["market"]["items"]}


def test_a_report_row_with_its_terms(seeded):
    c, rid, _ = seeded
    j, src, mism = _get(c, rid, "FX1")
    assert j["kind"] == "derivative" and j["valued"] and mism == []
    rep = src["positions"]
    assert rep["state"] == "present" and rep["state_text"] == "есть" and rep["level_text"] == "по сделке"
    (item,) = rep["items"]
    assert item["dataset_name"] == "Позиции" and item["source"] == "positions_csv"
    assert item["file_name"] == "positions_20261020.csv" and item["report_date"] == "2026-10-20" and item["loaded_at"]
    assert item["ref"] == "row FX1" and item["deal_id"] == "T1"
    terms = src["trade_terms"]
    assert terms["state"] == "present" and [i["trade_id"] for i in terms["items"]] == ["T1"]
    assert terms["items"][0]["dataset_name"] == "Условия сделок" and terms["items"][0]["source"] == "terms_csv"
    assert src["trades"]["state"] == "absent"
    ocp = src["ocp"]
    assert ocp["level"] == "book_ccy" and ocp["level_text"] == "по книге и валюте" and ocp["state"] == "present"
    assert [(i["ccy"], i["has_row"], i["file_name"]) for i in ocp["items"]] == [("USD", True, "ocp_20261020.csv")]
    assert src["cash_balances"]["state"] == "absent" and src["cash_balances"]["items"][0]["has_row"] is False
    m = _market(j)
    assert j["market"]["basis"] == "used"
    assert m[("curve", "RUB RUONIA")]["state"] == "ok" and m[("curve", "USD FX")]["state"] == "ok"
    assert m[("curve", "USD FX")]["name"] == "USD: из своп-пойнтов" and m[("fx", "USD")]["state_text"] == "есть"


def test_a_report_row_without_terms(seeded):
    c, rid, _ = seeded
    j, src, mism = _get(c, rid, "FX2")
    assert src["positions"]["state"] == "present" and src["trade_terms"]["state"] == "absent"
    assert mism == ["report_no_terms"]
    assert j["mismatches"][0]["text"] == "есть в отчёте позиций, но условия сделки не загружены"


def test_terms_without_a_report_row_off_the_report(seeded):
    c, rid, _ = seeded
    j, src, mism = _get(c, rid, "B1|T7")
    assert j["off_report"] and src["positions"]["state"] == "absent"
    assert src["positions"]["text"] == "отчёт позиций книги загружен, строки этой позиции в нём нет"
    assert src["positions"]["items"][0]["file_name"] == "positions_20261020.csv"  # which report was looked in
    assert [i["trade_id"] for i in src["trade_terms"]["items"]] == ["T7"]
    assert mism == ["terms_no_report"]
    assert j["mismatches"][0]["text"] == "условия загружены, строки в отчёте позиций нет"


def test_a_service_trade_not_in_the_report(seeded):
    c, rid, _ = seeded
    j, src, mism = _get(c, rid, "N1")
    assert j["kind"] == "bond" and src["positions"]["state"] == "absent"
    assert src["trade_terms"]["state"] == "not_needed"
    t = src["trades"]
    assert t["state"] == "present"
    tr = next(i for i in t["items"] if i["trade_id"] == "tr-1")
    assert tr["trade_id"] == "tr-1" and tr["state"] == "applied" and tr["state_text"] == "в позициях"
    assert tr["origin"] == "file" and tr["file_name"] == "trades_20261020.csv" and tr["dataset_name"] == "Сделки дня"
    assert mism == ["trades_no_report"]
    assert j["mismatches"][0]["text"] == "пришла из списка сделок сервиса, но её нет в отчёте позиций"
    assert _market(j)[("quote", "quote")]["state"] == "stale"


def test_one_deal_on_several_report_rows(seeded):
    c, rid, _ = seeded
    j, _, mism = _get(c, rid, "D1")
    assert "deal_on_rows" in mism
    assert "строк: 2" in next(m["text"] for m in j["mismatches"] if m["code"] == "deal_on_rows")


def test_a_missing_curve_on_a_valued_deal(seeded):
    c, rid, _ = seeded
    j, _, _ = _get(c, rid, "FX3")
    m = _market(j)
    assert m[("curve", "CNY FX")]["state"] == "missing"
    assert m[("curve", "CNY XCCY")]["state"] == "replaced" and "CNY FX" in m[("curve", "CNY XCCY")]["text"]
    assert m[("curve", "CNY XCCY")]["state_text"] == "заменена"
    assert m[("curve", "USD FX")]["state"] == "ok"   # only the curve of the currency that fell back is replaced


def test_a_missing_curve_on_an_unvalued_deal_names_every_need(seeded):
    c, rid, _ = seeded
    j, src, mism = _get(c, rid, "CCS1")
    assert not j["valued"] and j["market"]["basis"] == "needed" and mism == []
    assert {i["trade_id"] for i in src["trade_terms"]["items"]} == {"T3"}
    m = _market(j)
    assert m[("curve", "RUB RUONIA")]["state"] == "ok"
    assert m[("fx", "CNY")]["state"] == "ok"                      # the spot is there
    cny = m[("curve", "CNY XCCY")]                                 # a CCS leg: the XCCY curve first, then the others
    assert cny["state"] == "missing" and cny["state_text"] == "нет" and "CNY FX" in cny["text"]
    assert m[("fixing", "RUONIA")]["state"] == "ok"               # the floating RUB leg's index (a bridged day is no gap)
    assert any("кривой валюты сделки" in n for n in j["market"]["notes"])  # the reason in the coverage's words


def test_a_valued_bond(seeded):
    c, rid, _ = seeded
    j, src, mism = _get(c, rid, "BND")
    assert j["kind"] == "bond" and src["trade_terms"]["state"] == "not_needed"
    assert mism == ["report_trades_gap"]   # the run flagged its report against the start of the day and the trades
    assert j["mismatches"][0]["text"] == "количество в отчёте позиций не сходится с началом дня и сделками дня"
    m = _market(j)
    assert m[("curve", "RUB OFZ")]["name"] == "ОФЗ (КБД MOEX)" and m[("quote", "quote")]["state"] == "ok"
    # a RUB position has no currency of the OCP: the book's OCP report itself, by book only
    assert src["ocp"]["state"] == "present" and [i["file_name"] for i in src["ocp"]["items"]] == ["ocp_20261020.csv"]
    assert "ccy" not in src["ocp"]["items"][0]


def test_an_old_run_without_lineage_says_no_information(seeded):
    c, _, old = seeded
    j, src, mism = _get(c, old, "FX2")
    assert src["positions"]["state"] == "present"          # the run's own sets: always known
    assert src["trade_terms"]["state"] == "unknown" and src["trade_terms"]["state_text"] == "нет сведений"
    assert src["trades"]["state"] == "unknown"
    assert j["market"]["basis"] == "unknown" and j["market"]["items"] == []
    assert mism == []


def test_unknown_run_or_position_is_404(seeded):
    c, rid, _ = seeded
    r = c.get("/api/v1/positions/provenance", params={"id": "nope", "run_id": rid})
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert c.get("/api/v1/positions/provenance", params={"id": "FX1", "run_id": 999999}).status_code == 404
    assert c.get("/api/v1/positions/provenance").status_code == 422   # the id is required
    tag = c.get("/api/v1/positions/provenance", params={"id": "FX1", "run_id": rid}).headers["ETag"]
    again = c.get("/api/v1/positions/provenance", params={"id": "FX1", "run_id": rid}, headers={"If-None-Match": tag})
    assert again.status_code == 304


def test_a_published_run_names_what_each_derivative_used(published):  # noqa: F811 - the imported fixture
    """The whole path (``test_deriv_e2e_555``: derivatives valued by core from their terms, published into the store):
    the market data a valued deal's card names are those its valuation used, each present."""
    c, rid, _ = published
    for pid in ("DB|F1", "DB|N3", "DB|C5"):
        j, src, _ = _get(c, rid, pid)
        assert j["valued"] and j["kind"] == "derivative" and j["market"]["basis"] == "used", pid
        m = _market(j)
        assert m[("curve", "RUB RUONIA")]["state"] == "ok", (pid, m)
        assert src["positions"]["state"] == "absent"   # these inputs come with no positions set of the store
    m = _market(_get(c, rid, "DB|N3")[0])
    assert m[("fx", "USD")]["state"] == "ok"
    assert any(k[0] == "fixing" for k in m), m   # the NDF's fixing
    m = _market(_get(c, rid, "DB|F1")[0])
    assert any(k[0] == "curve" and k[1].startswith("CNY") for k in m), m


def test_a_derivative_core_did_not_value_is_not_valued_whatever_its_fallback_value(seeded):
    c, rid, _ = seeded
    j, _, _ = _get(c, rid, "CCS2")   # the report's MV, core refused the terms
    assert not j["valued"] and j["market"]["basis"] == "needed"
    m = _market(j)
    assert m[("curve", "RUB RUONIA")]["state"] == "ok" and m[("fx", "CNY")]["state"] == "ok"
    assert m[("curve", "CNY XCCY")]["state"] == "missing" and m[("fixing", "RUONIA")]["state"] == "ok"
    notes = " ".join(j["market"]["notes"])
    assert "условия сделки противоречивы" in notes and "оценка не по условиям сделки: по отчёту банка" in notes
    j, _, mism = _get(c, rid, "FX2")   # an FX forward without terms: the notional, PV 0
    assert not j["valued"] and j["market"]["basis"] == "needed" and mism == ["report_no_terms"]
    m = _market(j)
    assert m[("fx", "USD")]["state"] == "ok" and m[("curve", "USD FX")]["state"] == "ok"
    assert any("условий сделки нет" in n for n in j["market"]["notes"])


def test_the_foreign_curve_is_the_one_the_valuation_would_take(seeded, store_url):
    c, rid, _ = seeded
    m = _market(_get(c, rid, "FX4")[0])   # neither CNY curve: both named
    assert m[("curve", "CNY FX")]["state"] == "missing" and "CNY XCCY" in m[("curve", "CNY FX")]["text"]

    def rerun(fx: dict) -> int:   # the same inputs on a market of other curves (a complete run never changes)
        with store.connect(store_url) as con:
            lin, sets = con.execute("SELECT lineage, position_set_ids FROM run WHERE run_id = %s", (rid,)).fetchone()
            r = _run(con, {**lin, "fx": {**lin["fx"], **fx}}, sets)
            _pos(con, r, "FX4", 1, reason="no_foreign_curve: synthetic")
            _pos(con, r, "CCS1", 2, reason="no_foreign_curve: synthetic")
            con.execute("UPDATE run SET status = 'complete', completed_at = now() WHERE run_id = %s", (r,))
        return r

    xccy = {"curves": {**LINEAGE_FX["curves"], "CNY XCCY": {"status": "ok"}}}   # XCCY built, FX-implied not
    r = rerun(xccy)
    m = _market(_get(c, r, "FX4")[0])   # an FX forward on fx_implied: XCCY is its fallback
    assert m[("curve", "CNY XCCY")]["state"] == "replaced" and "CNY FX" in m[("curve", "CNY XCCY")]["text"]
    assert _market(_get(c, r, "CCS1")[0])[("curve", "CNY XCCY")]["state"] == "ok"   # a CCS leg: XCCY first
    r = rerun({**xccy, "options": {"foreign_curve": "xccy"}})   # the setting fx_foreign_curve = xccy
    assert _market(_get(c, r, "FX4")[0])[("curve", "CNY XCCY")]["state"] == "ok"


def test_a_row_without_asset_class_is_a_derivative_only_when_terms_name_it(seeded):
    c, rid, _ = seeded
    j, src, mism = _get(c, rid, "NA1")   # a deal_id no terms have: a bond, as the valuation took it
    assert j["kind"] == "bond" and src["trade_terms"]["state"] == "not_needed" and mism == []
    j, src, _ = _get(c, rid, "NA2")
    assert j["kind"] == "derivative" and [i["trade_id"] for i in src["trade_terms"]["items"]] == ["T10"]


def test_an_ndf_fixing_of_the_business_date_is_pending(seeded):
    c, rid, _ = seeded
    f = _market(_get(c, rid, "NDF1")[0])[("fixing", "USD 2026-10-20")]
    assert f["state"] == "pending" and f["state_text"] == "ещё не наступил"


def test_index_fixings_states():
    from desk.api import provenance as pv

    lin = {"rates": LINEAGE_RATES, "fx": {"index_fixings": {
        "RUSFARCNY": {"rows": 9, "latest": "2026-10-16", "unpublished_business_days": ["2026-10-19"]}}}}
    assert pv._fixings_state(lin, "RUONIA")[0] == "ok"            # bridged days inside the history: no gap
    assert pv._fixings_state(lin, "RUSFARCNY")[0] == "stale"      # the latest carried over unpublished days
    assert pv._fixings_state(lin, "SOFR")[0] == "missing"
    assert pv._fixings_state({}, "RUONIA")[0] == "unknown"


def test_trades_of_the_book_their_version_and_earlier_dates(seeded):
    c, rid, _ = seeded
    _, src, _ = _get(c, rid, "N1")
    by = {i["trade_id"]: i for i in src["trades"]["items"]}
    assert set(by) == {"tr-0", "tr-1", "tr-2"}
    assert by["tr-1"]["state"] == "applied"
    assert by["tr-2"]["state"] == "pending" and by["tr-2"]["state_text"] == "ждёт прогона"   # version 3, the run took 2
    assert by["tr-0"]["state"] == "earlier_date" and "2026-10-19" in by["tr-0"]["state_text"]
    assert all(i["feed_title"] is None for i in by.values())   # report folders, not a feed
    _, src, _ = _get(c, rid, "FX1")   # T1 of another book is not this deal
    assert src["trades"]["state"] == "absent"


def test_a_valued_ccs_on_a_curve_it_does_not_want_first_shows_it_replaced(seeded):
    c, rid, _ = seeded
    m = _market(_get(c, rid, "CCS3")[0])   # no XCCY curve in the run: the CCS took the FX-implied one
    assert m[("curve", "CNY FX")]["state"] == "replaced" and "CNY XCCY" in m[("curve", "CNY FX")]["text"]
    assert m[("curve", "RUB RUONIA")]["state"] == "ok"


def test_a_trade_amended_into_another_book_is_not_the_positions(seeded):
    c, rid, _ = seeded
    _, src, _ = _get(c, rid, "N1")
    assert "tr-3" not in {i["trade_id"] for i in src["trades"]["items"]}


def test_a_position_takes_only_its_own_legs_of_a_trade(seeded):
    c, rid, _ = seeded
    _, src, _ = _get(c, rid, "FXA")
    assert [(i["trade_id"], i["leg_id"]) for i in src["trade_terms"]["items"]] == [("T12", "a")]


def test_an_fx_future_by_ticker_needs_no_terms(seeded):
    c, rid, _ = seeded
    j, src, mism = _get(c, rid, "FUT1")
    assert j["valued"] and src["trade_terms"]["state"] == "not_needed" and mism == []
    assert "тикеру" in src["trade_terms"]["text"]


def test_fixings_are_needed_once_a_floating_period_has_started(seeded):
    c, rid, _ = seeded
    m = _market(_get(c, rid, "IRS1")[0])   # starts after the business date: no fixing yet
    assert not any(k[0] == "fixing" for k in m), m
    m = _market(_get(c, rid, "IRS2")[0])   # started: the key rate's fixings and its curve
    assert m[("fixing", "KEY_RATE")]["state"] == "ok" and m[("curve", "RUB KEY")]["state"] == "ok"
    m = _market(_get(c, rid, "REPO1")[0])  # a REPO on RUSFAR is not valued: no «фиксинги RUSFAR — есть»
    assert m[("fixing", "RUSFAR")]["state"] == "missing" and m[("fixing", "RUSFAR")]["name"] == "индекс RUSFAR"


def test_one_deal_on_rows_says_why_only_for_ambiguous_terms():
    from desk.api import provenance as pv

    assert pv._mismatch_text("deal_on_rows", 2, "terms_ambiguous: T5") == (
        "одна сделка на нескольких строках отчёта позиций: условия не отнести к одной строке (строк: 2)")
    assert pv._mismatch_text("deal_on_rows", 3, None) == "одна сделка на нескольких строках отчёта позиций (строк: 3)"
    assert pv._mismatch_text("deal_on_rows", 1, "terms_ambiguous") == (
        "одна сделка на нескольких строках отчёта позиций: условия не отнести к одной строке")


def test_a_current_run_without_trades_lineage_says_why(seeded, store_url):
    c, rid, _ = seeded
    with store.connect(store_url) as con:
        lin, sets = con.execute("SELECT lineage, position_set_ids FROM run WHERE run_id = %s", (rid,)).fetchone()
        lin = {k: v for k, v in lin.items() if k != "intraday_trades"}
        quiet = _run(con, lin, sets, day=date(2026, 10, 21), version="0.2.24")   # no trades sets of that date
        off = _run(con, lin, sets, version="0.2.24")   # trades of the date there: the setting was off
        for r in (quiet, off):
            _pos(con, r, "N1", 1, fact={"ccy": "RUB", "instr_type": "BOND", "risk": {"RUB OFZ": [1.0]},
                                        "extra": {"quality": "exact"}})
            con.execute("UPDATE run SET status = 'complete', completed_at = now() WHERE run_id = %s", (r,))
    t = _get(c, quiet, "N1")[1]["trades"]
    assert t["state"] == "absent" and t["text"].startswith("нет данных: сделок дня на эту дату")
    t = _get(c, off, "N1")[1]["trades"]
    assert t["state"] == "present" and "выключен" in t["text"]
    assert {i["state"] for i in t["items"] if i["trade_id"] == "tr-1"} == {"off"}


def test_a_trade_moved_to_another_position_is_not_this_ones(seeded):
    c, rid, _ = seeded
    _, src, _ = _get(c, rid, "N1")
    assert "tr-4" not in {i["trade_id"] for i in src["trades"]["items"]}


def test_any_non_bond_row_with_terms_is_a_derivative(seeded):
    c, rid, _ = seeded
    j, src, _ = _get(c, rid, "FXT")   # value date = the business date: «other» by its date, but terms link to it
    assert j["kind"] == "derivative" and not j["valued"] and j["market"]["basis"] == "needed"
    assert [i["trade_id"] for i in src["trade_terms"]["items"]] == ["T16"]


def test_an_old_run_without_rate_statuses_is_unknown():
    from desk.api import provenance as pv

    assert pv._rub_state({"rates": {"ruonia_ois": {"rows": 3}}}, "ruonia_ois") == "unknown"
    assert pv._rub_state({}, "ruonia_ois") == "unknown"
    assert pv._rub_state({"rates": {"status": {"ruonia_ois": "missing"}}}, "ruonia_ois") == "missing"


def test_an_unvalued_fx_future_needs_no_terms(seeded):
    c, rid, _ = seeded
    j, src, mism = _get(c, rid, "FUT2")
    assert not j["valued"] and src["trade_terms"]["state"] == "not_needed" and mism == []


def test_an_unvalued_derivative_without_terms_says_the_list_is_short(seeded):
    c, rid, _ = seeded
    j, _, _ = _get(c, rid, "FX2")
    assert any(n.startswith("условия сделки не загружены: список неполный") for n in j["market"]["notes"])
