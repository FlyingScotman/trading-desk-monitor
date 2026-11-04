"""One scope rule on synthetic data (TDM#566, #496): under one header filter (book, portfolio, without HTM, currency,
entity, desk) the PnL report's total is the sum of its own list, and both equal the sum of the positions the
shared cases (``tests/js/fixtures/scope_cases.json``) expect; the client's predicate (``tests/js/scope_566.test.js``)
returns the same positions. The effective portfolio: the HTM type of the book first, else the report's column of the
latest set that has it, else none; the SQL form of the rule equals the Python form."""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from desk import store
from desk.api import create_app
from desk.api.runs import PNL_KEYS
from desk.api.scope_rule import EFFECTIVE_PORTFOLIO_SQL, portfolio_of

CASES = json.loads((Path(__file__).parent.parent.parent / "tests" / "js" / "fixtures" / "scope_cases.json")
                   .read_text(encoding="utf-8"))
DAY = "2026-10-16"


def seed(url, *, reverse=False, extras=None):
    """One run with the cases' books and positions; a position's ``reports`` are its rows in successive sets (a
    ``null`` entry is a row without the Portfolio column); ``reverse``: the rows are inserted in the opposite order (the sets keep theirs); ``extras``: ``{id: (book, fact.extra)}``
    of rows without an input row, or the extra of a case's row."""
    extras = extras or {}
    with store.connect(url) as con:
        rid = con.execute(
            "INSERT INTO run(run_type,business_date,prev_date,cut_type,market_snapshot_id,static_snapshot_id,"
            "private_inputs_hash,position_set_ids,positions_known_at,core_version,backend_version,lineage)"
            " VALUES ('EOD',%s,'2026-10-15','eod_final','synthetic-market','synthetic-static','synthetic','{}',now(),"
            "'test','test','{}') RETURNING run_id", (DAY,)).fetchone()[0]
        books = [{"book_id": b, "name": b, "entity": m["entity"], "desk": m["desk"],
                  **({"book_type": m["type"]} if m["type"] else {})} for b, m in CASES["books"].items()]
        con.execute("INSERT INTO run_reference VALUES (%s,'books',%s)", (rid, Jsonb(books)))
        n_sets = max(len(p["reports"]) for p in CASES["positions"])
        set_ids = {}
        for k in range(n_sets):  # the later set has the greater id
            for b in CASES["books"]:
                set_ids[b, k] = con.execute(
                    "INSERT INTO position_set(book_id,valid_date,revision,source_name,source_file_sha256)"
                    " VALUES (%s,%s,%s,'synthetic','synthetic') RETURNING set_id", (b, DAY, k + 1)).fetchone()[0]
        order = list(reversed(CASES["positions"])) if reverse else CASES["positions"]
        for i, p in enumerate(order):
            q = dict.fromkeys(PNL_KEYS, 0.0) | {"carry": float(p["pnl"]), "extra": {"method": "desk-flash-v1"}}
            fact = {"book_id": p["book"], "name": p["id"], "ccy": p["ccy"], "instr_type": "OTHER",
                    "asset_class": "other", "quantity": 1, "mv_rub": 1}
            if p["id"] in extras:
                fact["extra"] = extras[p["id"]][1]
            con.execute("INSERT INTO run_position(run_id,position_id,ord,book_id,secid,quantity,quality,fact,pnl)"
                        " VALUES (%s,%s,%s,%s,'S',1,'reported_value',%s,%s)",
                        (rid, p["id"], i, p["book"], Jsonb(fact), Jsonb(q)))
            for k, rep in (reversed(list(enumerate(p["reports"]))) if reverse else enumerate(p["reports"])):
                x = {"x_portfolio": rep} if rep is not None else {}
                con.execute("INSERT INTO position_input(set_id,position_id,secid,quantity,ccy,source_instrument_name,x)"
                            " VALUES (%s,%s,'S',1,%s,%s,%s)", (set_ids[p["book"], k], p["id"], p["ccy"], p["id"],
                                                               Jsonb(x)))
        for i, (pid, (book, extra)) in enumerate(extras.items()):
            if pid not in {p["id"] for p in CASES["positions"]}:  # no input row at all
                q = dict.fromkeys(PNL_KEYS, 0.0) | {"carry": 1.0, "extra": {"method": "desk-flash-v1"}}
                fact = {"book_id": book, "name": pid, "ccy": "RUB", "instr_type": "OTHER", "asset_class": "other",
                        "quantity": 1, "mv_rub": 1, "extra": extra}
                con.execute("INSERT INTO run_position(run_id,position_id,ord,book_id,secid,quantity,quality,fact,pnl)"
                            " VALUES (%s,%s,%s,%s,'S',1,'reported_value',%s,%s)",
                            (rid, pid, 100 + i, book, Jsonb(fact), Jsonb(q)))
        con.execute("UPDATE run SET position_set_ids=%s,status='complete',completed_at=now() WHERE run_id=%s",
                    (sorted(set_ids.values()), rid))
    return rid


def params(flt):
    out = {"from": DAY, "to": DAY}
    for k, name in (("entity", "entity"), ("desk", "desk"), ("ccy", "ccy"), ("books", "books"),
                    ("portsOff", "ports_off")):
        if flt.get(k):
            out[name] = flt[k]
    return out


@pytest.mark.parametrize("reverse", [False, True])
def test_report_total_is_the_sum_of_its_list_and_of_the_expected_positions(store_url, reverse):
    seed(store_url, reverse=reverse)
    c = TestClient(create_app(store_url))
    pnl = {p["id"]: p["pnl"] for p in CASES["positions"]}
    for case in CASES["filters"]:
        r = c.get("/api/v1/history/pnl", params=params(case["filter"]))
        assert r.status_code == 200, r.text
        out = r.json()
        ids = sorted(p["row"]["positionId"] for p in out["positions"])
        assert ids == sorted(case["expect"]), case["name"]
        listed = [p["total"] for p in out["positions"] if p["total"] is not None]
        expected = math.fsum(pnl[i] for i in case["expect"]) if case["expect"] else None
        assert out["summary"]["total"] == pytest.approx(expected), case["name"]
        assert math.fsum(listed) == pytest.approx(expected or 0.0), case["name"]
        assert sum(b["total"] for b in out["books"]) == pytest.approx(expected or 0.0), case["name"]
        assert math.fsum(d["total"] for d in out["days"] if d["total"] is not None) == pytest.approx(expected or 0.0)


@pytest.mark.parametrize("reverse", [False, True])
def test_effective_portfolio_in_every_server_form(store_url, reverse):
    rid = seed(store_url, reverse=reverse)
    c = TestClient(create_app(store_url))
    want = {p["id"]: p["portfolio"] for p in CASES["positions"]}
    books = c.get("/api/v1/concepts/run", params={"run_id": rid}).json()["BOOKS"]
    assert {b: m.get("portfolio") for b, m in books.items()} == {"B1": "HTM", "B2": None, "B3": None}
    cat = c.get("/api/v1/history/pnl", params=params({})).json()["catalogue"]
    assert {r["positionId"]: r["portfolio"] for r in cat} == want  # the PnL report
    for route in ("concepts/run", "compat/desk"):  # the monitors' run and its compat twin
        r = c.get(f"/api/v1/{route}", params={"run_id": rid})
        assert r.status_code == 200, (route, r.text)
        assert {p["id"]: p["portfolio"] for p in r.json()["positions"]} == want, route


def test_sql_and_python_forms_of_the_rule_agree(store_url):
    facts = (None, {"extra": {"x": {"x_portfolio": "F"}}}, {"extra": {"reported": {"x": {"x_portfolio": "R"}}}},
             {"extra": {"reported": "text"}})
    with store.connect(store_url) as con:
        for book_type in (None, "", "HTM", "htm", " Htm ", "\tHTM\n", "\x0bHtm\x0c", "TRADING", "DERIVS"):
            for reported in (None, "HTM", "Торговый"):
                for fact in facts:
                    got = con.execute(
                        f"SELECT {EFFECTIVE_PORTFOLIO_SQL} AS r FROM (SELECT %s::jsonb AS data) b,"
                        " (SELECT %s::text AS portfolio) ip, (SELECT %s::jsonb AS fact) p",
                        (Jsonb({"book_type": book_type} if book_type is not None else {}),
                         reported, Jsonb(fact))).fetchone()[0]
                    want = portfolio_of({("B", "P"): reported}, {"B": book_type}, "B", "P", fact)
                    assert got == want, (book_type, reported, fact)
        # a position of a book without a reference row: the report's column, no error
        assert con.execute(
            f"SELECT {EFFECTIVE_PORTFOLIO_SQL} FROM (SELECT 'A'::text AS portfolio) ip"
            " LEFT JOIN (SELECT NULL::jsonb AS data WHERE false) b ON true"
            " CROSS JOIN (SELECT NULL::jsonb AS fact) p").fetchone()[0] == "A"
    assert date.fromisoformat(DAY)


def test_rows_the_trades_built_take_their_portfolio_from_the_row(store_url):
    """``X|closed`` and a position the trades opened have no input row: the column kept in ``fact.extra`` (``x`` or
    ``reported.x``) stands for them; the input row, when there is one, and the HTM type of the book come first."""
    rid = seed(store_url, extras={
        "X|closed": ("B2", {"x": {"x_portfolio": "A"}}), "NEW": ("B2", {"reported": {"x": {"x_portfolio": "A"}}}),
        "HTMX": ("B1", {"x": {"x_portfolio": "A"}}), "P4": ("B2", {"x": {"x_portfolio": "A"}})})
    c = TestClient(create_app(store_url))

    def ids(**kw):
        return {p["row"]["positionId"] for p in c.get("/api/v1/history/pnl", params=params({}) | kw).json()["positions"]}
    assert {"X|closed", "NEW"} <= ids() and "X|closed" not in ids(ports_off="A") and "NEW" not in ids(ports_off="A")
    assert {"X|closed", "NEW"} <= ids(ports_off="B")
    assert "HTMX" in ids(ports_off="B") and "HTMX" not in ids(ports_off="HTM") and "HTMX" in ids(ports_off="A")
    assert "P4" in ids(ports_off="A") and "P4" not in ids(ports_off="Торговый")
    run = c.get("/api/v1/concepts/run", params={"run_id": rid}).json()
    assert {p["id"]: p["portfolio"] for p in run["positions"]} | {} == {
        **{p["id"]: p["portfolio"] for p in CASES["positions"]}, "X|closed": "A", "NEW": "A", "HTMX": "HTM"}
