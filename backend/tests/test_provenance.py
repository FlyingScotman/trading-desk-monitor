"""Where a position's data came from (TDM#617): ``GET /api/v1/positions/provenance``, the card's «Откуда данные». A run
written straight into the store with its sets: report rows with and without terms, terms without a report row (a
derivative off the report), a position the day's trades opened, one deal on two report rows, a valued and an unvalued
deal with a missing curve, an old run without lineage. Synthetic data only."""

from __future__ import annotations

import json
from datetime import date

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
                 "fixings": {"RUONIA": {"rows": 30, "latest": "2026-10-19"}, "KEY_RATE": {"rows": 30}}}


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


def _run(con, lineage: dict, set_ids: list[int]) -> int:
    rid = con.execute(
        "INSERT INTO run (run_type, business_date, prev_date, cut_type, market_snapshot_id, static_snapshot_id,"
        " private_inputs_hash, position_set_ids, positions_known_at, core_version, backend_version, lineage)"
        " VALUES ('EOD', %s, %s, 'eod_final', 'synthetic-market', 'synthetic-static', 'h', %s, now(), 't', 't', %s)"
        " RETURNING run_id", (BD, date(2026, 10, 19), set_ids, Jsonb(lineage))).fetchone()[0]
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
                        ("d", "ocp_20261020.csv")):
            _file(con, SHA[k], name)
        ps = con.execute(
            "INSERT INTO position_set (book_id, valid_date, revision, source_name, source_file_sha256, source,"
            " report_date, inbox_file_sha256) VALUES ('B1', %s, 1, 'positions_20261020.csv', %s, 'positions_csv', %s,"
            " %s) RETURNING set_id", (BD, SHA["a"], BD, SHA["a"])).fetchone()[0]
        for pid, cls, ccy, deal, vd in (("FX1", "fx", "USD", "T1", "2026-11-20"), ("FX2", "fx", "USD", "T9", "2026-11-20"),
                                        ("FX3", "fx", "EUR", "T4", "2026-11-20"), ("CCS1", "otc", "CNY", "T3", None),
                                        ("D1", "otc", "RUB", "T5", None), ("D2", "otc", "RUB", "T5", None),
                                        ("BND", "bond", "RUB", None, None)):
            con.execute("INSERT INTO position_input (set_id, position_id, secid, quantity, asset_class, ccy, deal_id,"
                        " value_date, src) VALUES (%s, %s, NULL, NULL, %s, %s, %s, %s, %s)",
                        (ps, pid, cls, ccy, deal, vd, f"row {pid}"))
        terms = [
            {"trade_id": "T1", "book_id": "B1", "product_kind": "fx_forward", "base_ccy": "USD", "quote_ccy": "RUB"},
            {"trade_id": "T3", "leg_id": "1", "book_id": "B1", "product_kind": "ccs", "ccy": "RUB", "index_code": "RUONIA"},
            {"trade_id": "T3", "leg_id": "2", "book_id": "B1", "product_kind": "ccs", "ccy": "CNY", "fixed_rate_pct": "3"},
            {"trade_id": "T4", "book_id": "B1", "product_kind": "fx_forward", "base_ccy": "EUR", "quote_ccy": "RUB"},
            {"trade_id": "T5", "book_id": "B1", "product_kind": "irs", "ccy": "RUB", "fixed_rate_pct": "10"},
            {"trade_id": "T7", "book_id": "B1", "product_kind": "fx_forward", "base_ccy": "USD", "quote_ccy": "RUB"},
        ]
        ts = _dataset(con, "trade_terms", SHA["b"], "terms_20261020.csv", terms, source="terms_csv")
        tr = _dataset(con, "trades", SHA["c"], "trades_20261020.csv", [
            {"trade_id": "tr-1", "trade_version": 1, "status": "live", "book_id": "B1", "position_id": "N1",
             "asset_class": "bond", "side": "buy", "trade_date": BD.isoformat(), "quantity": "5"}], source="trades_csv")
        _dataset(con, "ocp", SHA["d"], "ocp_20261020.csv", [{"book_id": "B1", "entity": "E1", "ccy": "USD"}],
                 source="ocp_csv", scope={"entity": "E1", "book_id": "B1"})
        lineage = {"positions": {"set_ids": [ps], "trade_terms": [{"scope": {"book_id": "B1"}, "set_id": ts}],
                                 "off_report": {"list": [{"book": "B1", "trade_id": "T7", "state": "included"}]}},
                   "rates": LINEAGE_RATES, "fx": LINEAGE_FX, "stale_datasets": {},
                   "intraday_trades": {"set_ids": [tr], "versions": {"tr-1": 1}, "skipped_ids": {}}}
        rid = _run(con, lineage, [ps])
        core = {"rub_curve": "RUB RUONIA", "foreign_curves": ["USD FX"], "trades": ["T1"]}
        _pos(con, rid, "FX1", 1, fact={"ccy": "USD", "fx_delta": {"USD": 1.0},
                                       "extra": {"quality": "exact", "core": core, "fx_rates": {"USD": 80.0}}})
        _pos(con, rid, "FX2", 2, fact={"ccy": "USD", "extra": {"quality": "assumed",
                                                               "not_exact_reason": "no_trade_terms: synthetic"}})
        _pos(con, rid, "FX3", 3, fact={"ccy": "EUR", "extra": {"quality": "assumed", "core": {
            "rub_curve": "RUB RUONIA", "foreign_curves": ["USD FX"], "trades": ["T4"],
            "curve_fallback_from": ["EUR FX"]}, "fx_rates": {"EUR": 90.0}}})
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
    (tr,) = t["items"]
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
    assert m[("curve", "EUR FX")]["state"] == "missing"
    assert m[("curve", "USD FX")]["state"] == "replaced" and "EUR FX" in m[("curve", "USD FX")]["text"]
    assert m[("curve", "USD FX")]["state_text"] == "заменена"


def test_a_missing_curve_on_an_unvalued_deal_names_every_need(seeded):
    c, rid, _ = seeded
    j, src, mism = _get(c, rid, "CCS1")
    assert not j["valued"] and j["market"]["basis"] == "needed" and mism == []
    assert {i["trade_id"] for i in src["trade_terms"]["items"]} == {"T3"}
    m = _market(j)
    assert m[("curve", "RUB RUONIA")]["state"] == "ok"
    assert m[("fx", "CNY")]["state"] == "ok"                      # the spot is there
    assert m[("curve", "CNY FX")]["state"] == "missing"           # the curve is not: not just the first failure
    assert m[("curve", "CNY FX")]["state_text"] == "нет"
    assert m[("fixing", "RUONIA")]["state"] == "ok"               # the floating RUB leg's index
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
