"""TDM#753: the card's «Откуда данные» says why a derivative row of the positions report has no terms
(``report_no_terms`` with a sub-code ``detail``): none in the store (``no_terms``), only in an older set the current one
dropped (``superseded``), only in another book (``other_book``), in this book but in a set the run did not read
(``set_not_read``), in a set the run read but not linked (``not_linked``). Synthetic data only."""

from __future__ import annotations

import json
from datetime import date

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_provenance import BD, LINEAGE_FX, LINEAGE_RATES, SHA, _file, _pos, _run

from desk import store
from desk.api import create_app

PID, DEAL = "FX2", "T9"
OTHER_TERMS = {"trade_id": "T1", "book_id": "B1", "product_kind": "fx_forward", "base_ccy": "USD", "quote_ccy": "RUB"}
MINE = {"trade_id": DEAL, "book_id": "B1", "product_kind": "fx_forward", "base_ccy": "USD", "quote_ccy": "RUB"}


def _terms_set(con, rows: list[dict], *, book: str = "B1", valid: date = BD, recorded: str = "now() - interval '1 hour'",
               name: str = "terms.csv", source: str = "terms_csv",
               revision: int = 1) -> int:
    scope = {"book_id": book}
    sid = con.execute(
        "INSERT INTO dataset_set (dataset, scope, scope_key, valid_date, revision, source, report_date, inbox_file_sha256,"
        " source_name, adapter_fingerprint, unit_hash, row_count, recorded_at) VALUES ('trade_terms', %s, %s, %s, %s, %s,"
        f" %s, %s, %s, 'fp', %s, %s, {recorded}) RETURNING set_id",
        (Jsonb(scope), json.dumps(scope, sort_keys=True), valid, revision, source, valid, SHA["b"], name,
         f"uh-{name}-{book}-{valid}", len(rows))).fetchone()[0]
    for n, r in enumerate(rows, 1):
        con.execute("INSERT INTO dataset_row (set_id, row_no, row_key, data) VALUES (%s, %s, %s, %s)",
                    (sid, n, str(n), Jsonb(r)))
    return sid


def _env(store_url: str, run_rows: list[dict], *, before: list[tuple] = (), after: list[tuple] = ()) -> tuple[TestClient, int]:
    """A run of 20.10 of book B1 with one FX forward row FX2 (deal T9) and the terms set ``run_rows`` it read. ``before``:
    other sets stored before the run, ``after``: sets stored after it (own transaction), each (rows, kwargs of
    ``_terms_set``)."""
    with store.connect(store_url) as con:
        _file(con, SHA["a"], "positions.csv")
        _file(con, SHA["b"], "terms.csv")
        ps = con.execute(
            "INSERT INTO position_set (book_id, valid_date, revision, source_name, source_file_sha256, source, report_date,"
            " inbox_file_sha256) VALUES ('B1', %s, 1, 'positions.csv', %s, 'positions_csv', %s, %s) RETURNING set_id",
            (BD, SHA["a"], BD, SHA["a"])).fetchone()[0]
        con.execute("INSERT INTO position_input (set_id, position_id, secid, quantity, asset_class, ccy, deal_id,"
                    " value_date, src) VALUES (%s, %s, NULL, NULL, 'fx', 'USD', %s, '2026-11-20', 'row')", (ps, PID, DEAL))
        for rows, kw in before:
            _terms_set(con, rows, **kw)
        ts = _terms_set(con, run_rows, name="run.csv")
        lineage = {"positions": {"set_ids": [ps], "trade_terms": [{"scope": {"book_id": "B1"}, "set_id": ts}]},
                   "rates": LINEAGE_RATES, "fx": LINEAGE_FX, "stale_datasets": {}}
        rid = _run(con, lineage, [ps])
        _pos(con, rid, PID, 1, fact={"ccy": "USD", "extra": {"quality": "assumed",
                                                             "not_exact_reason": "no_trade_terms: synthetic"}})
        con.execute("UPDATE run SET status = 'complete', completed_at = now() WHERE run_id = %s", (rid,))
    if after:
        with store.connect(store_url) as con:   # a later transaction: recorded after the run read its inputs
            for rows, kw in after:
                _terms_set(con, rows, **{"recorded": "now()", **kw})
    return TestClient(create_app(store_url)), rid


def _gap(c: TestClient, rid: int) -> dict:
    r = c.get("/api/v1/positions/provenance", params={"id": PID, "run_id": rid})
    assert r.status_code == 200, r.text
    (m,) = [m for m in r.json()["mismatches"] if m["code"] == "report_no_terms"]
    assert m["text"] == "есть в отчёте позиций, но условия сделки не загружены: " + m["detail_text"]
    return m


def test_no_terms_of_this_trade_anywhere(store_url):
    c, rid = _env(store_url, [OTHER_TERMS])
    m = _gap(c, rid)
    assert m["detail"] == "no_terms"
    assert "deal_id «T9»" in m["detail_text"] and "номером позиции «FX2»" in m["detail_text"]
    assert "ни в одной книге" in m["detail_text"]


def test_terms_only_in_an_older_set_the_current_one_dropped(store_url):
    old = date(2026, 10, 19)   # an earlier valid date: the run's set (of 20.10) is the current one of the book
    c, rid = _env(store_url, [OTHER_TERMS], before=[([MINE], {"valid": old, "name": "old.csv"})])
    m = _gap(c, rid)
    assert m["detail"] == "superseded"
    assert "«B1»" in m["detail_text"] and "19.10.2026" in m["detail_text"] and "не содержит" in m["detail_text"]


def test_terms_in_another_book(store_url):
    c, rid = _env(store_url, [OTHER_TERMS], before=[([{**MINE, "book_id": "B2"}], {"book": "B2", "name": "b2.csv"})])
    m = _gap(c, rid)
    assert m["detail"] == "other_book"
    assert "в другой книге: «B2»" in m["detail_text"] and "позиция в книге «B1»" in m["detail_text"]


def test_terms_of_this_book_in_a_set_the_run_did_not_read(store_url):
    c, rid = _env(store_url, [OTHER_TERMS], after=[([MINE, OTHER_TERMS], {"name": "late.csv", "revision": 2})])
    m = _gap(c, rid)
    assert m["detail"] == "set_not_read"
    t = m["detail_text"]
    assert "книге «B1»" in t and "набор от 20.10.2026" in t and "МСК" in t and "набор пришёл позже" in t
    assert "монитор пересчитает дату сам при ближайшем цикле автопилота (Настройки → «Автообновление»)" in t
    assert t.endswith("кнопка «Обновить» в строке «Автопилот» вверху главной страницы и страницы «Настройки» "
                      "(у роли «просмотр» её нет)")


def test_a_set_that_was_there_before_the_run_but_not_read_has_no_recompute_button(store_url):
    # revision 2 of the book's unit, recorded after the run's set but before the run: current now, not what the run read
    c, rid = _env(store_url, [OTHER_TERMS], before=[([MINE], {"revision": 2, "name": "other.csv",
                                                              "recorded": "now() - interval '30 minutes'"})])
    m = _gap(c, rid)
    assert m["detail"] == "set_not_read" and "набор был, но прогон читал другие наборы книги" in m["detail_text"]
    assert "отдельной кнопки пересчёта одной даты в мониторе нет" in m["detail_text"]
    assert "пришёл позже" not in m["detail_text"]


def test_a_run_that_read_the_trade_is_explained_by_the_link_even_if_a_newer_set_exists(store_url):
    c, rid = _env(store_url, [{**MINE, "product_kind": "bond_purchase"}], after=[([MINE], {"revision": 2})])
    assert _gap(c, rid)["detail"] == "not_linked"   # not_linked goes before set_not_read


def test_the_superseded_search_is_bounded_to_the_last_month_of_the_book(store_url):
    too_old = date(2026, 9, 1)   # 49 days before the run's date
    c, rid = _env(store_url, [OTHER_TERMS], before=[([MINE], {"valid": too_old, "name": "old.csv"})])
    m = _gap(c, rid)
    assert m["detail"] == "no_terms" and "за последние 31 дн." in m["detail_text"]


def test_terms_of_the_run_that_the_link_did_not_take(store_url):
    c, rid = _env(store_url, [{**MINE, "product_kind": "bond_purchase"}])   # a kind the link does not attach
    m = _gap(c, rid)
    assert m["detail"] == "not_linked"
    t = m["detail_text"]
    assert "trade_id условий сравнивался с deal_id «T9» и с номером позиции «FX2»" in t
    assert "trade_id «T9», вид продукта «bond_purchase» (такой вид к строкам отчёта не привязывается)" in t


def test_a_leg_naming_another_position_is_not_linked(store_url):
    c, rid = _env(store_url, [{**MINE, "position_id": "OTHERPOS"}])
    m = _gap(c, rid)
    assert m["detail"] == "not_linked" and "названа другой позицией «OTHERPOS»" in m["detail_text"]
