"""TDM#753: ``desk.terms_plan.plan`` holds a trade's terms only in the trade's own book, the key the run links them by
(``derivatives.trade_key``). Terms stored in another book are no terms of this trade: it stays ``need`` and the adapter
is asked for it again. Synthetic data only."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from test_terms_plan_561 import D, _fresh_process, _put, _settings, con, inbox  # noqa: F401 - the fixtures of #561

from desk import terms_plan
from desk.adapter_services import write_request
from desk.store import DatasetInputs
from desk.valuation import derivatives as dv

HEAD = ("trade_id;trade_version;book_id;product_kind;trade_date;value_date;base_ccy;quote_ccy;side;"
        "notional_base;notional_quote;contract_rate\n")


def _terms(*rows: tuple[str, str, str]) -> str:
    return HEAD + "".join(f"{t};{v};{b};fx_forward;2026-10-01;2026-12-17;USD;RUB;buy;10;800;80\n"
                          for t, v, b in rows)


def test_terms_in_another_book_are_no_terms_of_the_trade(con, tmp_path: Path, inbox: Path) -> None:  # noqa: F811
    s = _settings(tmp_path, inbox)
    # NDF-1 is listed in D1, its terms are stored in T1 only; NDF-2 has its terms in D1
    _put(con, s, inbox, "trade_terms_20261020.csv", _terms(("NDF-1", "1", "T1"), ("NDF-2", "1", "D1")))
    _put(con, s, inbox, "trade_list_20261020.csv",
         "trade_id;trade_version;book_id;status\r\nNDF-1;1;D1;live\r\nNDF-2;1;D1;live\r\nOTHER;1;T1;live\r\n")
    p = terms_plan.plan(con, D("2026-10-20"), ["D1", "T1"])
    need = {(t["book_id"], t["trade_id"]): t["need"] for t in p["trades"]}
    assert need == {("D1", "NDF-1"): True, ("D1", "NDF-2"): False, ("T1", "OTHER"): True}
    assert p["new"] == ["NDF-1", "OTHER"] and p["have"] == ["NDF-2"] and p["changed"] == []
    assert p["closed"] == []  # NDF-1 is live (in D1): its terms in T1 are no terms of it, but the id is not closed
    assert p["counts"]["requested"] == 2 and p["counts"]["have"] == 1
    # the same book: the trade is held
    _put(con, s, inbox, "trade_terms_20261021.csv", _terms(("NDF-1", "1", "D1"), ("NDF-2", "1", "D1")))
    _put(con, s, inbox, "trade_list_20261021.csv",
         "trade_id;trade_version;book_id;status\r\nNDF-1;1;D1;live\r\nNDF-2;1;D1;live\r\nOTHER;2;T1;live\r\n")
    p = terms_plan.plan(con, D("2026-10-21"), ["D1", "T1"])
    assert {t["trade_id"]: t["need"] for t in p["trades"]} == {"NDF-1": False, "NDF-2": False, "OTHER": True}
    assert p["have"] == ["NDF-1", "NDF-2"]


def _two_books(monkeypatch, day, versions: tuple[int, int], terms_book: str):
    units = [{"scope": {"book_id": b}, "valid_date": day, "set_id": i} for i, b in enumerate(("D1", "T1"))]
    lists = DatasetInputs("trade_list", day, [{"trade_id": "1001", "trade_version": v, "book_id": b, "status": "live"}
                                              for b, v in zip(("D1", "T1"), versions)], units)
    terms = DatasetInputs("trade_terms", day, [{"trade_id": "1001", "trade_version": 1, "book_id": terms_book}], [])
    monkeypatch.setattr(terms_plan.store, "dataset_rows", lambda _c, ds, on, **kw: lists if ds == "trade_list" else terms)
    return terms_plan.plan(None, day, ["D1", "T1"])


def test_a_trade_listed_in_two_books_is_one_entry_with_the_need_of_its_own_book(monkeypatch) -> None:
    """One ``trade_id`` live in two books: the adapter refuses a request that names it twice, so one entry stays (the
    higher version, else the first book), needing terms unless its own book holds them."""
    day = D("2026-10-20")
    p = _two_books(monkeypatch, day, (1, 1), "D1")   # the same version: the first book, D1, holds the terms
    assert [(t["book_id"], t["need"]) for t in p["trades"]] == [("D1", False)] and p["live"] == 1
    p = _two_books(monkeypatch, day, (1, 2), "D1")   # T1's is the higher version: its book holds none
    assert [(t["book_id"], t["need"], t["trade_version"]) for t in p["trades"]] == [("T1", True, 2)]
    assert p["new"] == ["1001"] and p["have"] == [] and p["closed"] == []


def test_the_request_file_follows_the_adapters_contract(con, tmp_path: Path, inbox: Path) -> None:  # noqa: F811
    """08 §2.2: the file is the plan's trades of the listed books, one entry per ``trade_id`` (the adapter refuses a
    request naming one twice), every ``book_id`` one of ``books``, ids as digits."""
    s = _settings(tmp_path, inbox)
    _put(con, s, inbox, "trade_terms_20261020.csv", _terms(("1001", "1", "T1"), ("1002", "1", "D1")))
    _put(con, s, inbox, "trade_list_20261020.csv",
         "trade_id;trade_version;book_id;status\r\n1001;1;D1;live\r\n1002;1;D1;live\r\n1003;;T1;live\r\n"
         "1004;1;T1;cancelled\r\n")
    plan = terms_plan.plan(con, D("2026-10-20"), ["D1", "T1"])
    path = write_request(SimpleNamespace(home=tmp_path / "home"), "run1", plan)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["format"] == "terms-request" and doc["version"] == 1 and doc["date"] == "2026-10-20"
    ids = [t["trade_id"] for t in doc["trades"]]
    assert len(ids) == len(set(ids)) == 3 and all(i.isdigit() for i in ids)
    assert all(t["book_id"] in doc["books"] for t in doc["trades"]) and doc["books"] == ["D1", "T1"]
    assert {t["trade_id"]: t["need"] for t in doc["trades"]} == {"1001": True, "1002": False, "1003": True}
    assert all(set(t) == {"trade_id", "book_id", "trade_version", "need"} for t in doc["trades"])


def test_terms_of_the_books_own_set_stored_after_the_list_are_held(con, tmp_path: Path, inbox: Path) -> None:  # noqa: F811
    """A terms set of the right book that arrives later (after a run read the older inputs) is «held»: the adapter is not
    asked again; the run is made again by the autopilot (private inputs changed), not by the plan."""
    s = _settings(tmp_path, inbox)
    _put(con, s, inbox, "trade_list_20261020.csv", "trade_id;trade_version;book_id;status\r\n1001;1;D1;live\r\n")
    assert [t["need"] for t in terms_plan.plan(con, D("2026-10-20"), ["D1"])["trades"]] == [True]
    _put(con, s, inbox, "trade_terms_20261020.csv", _terms(("1001", "1", "D1")))   # the later set
    p = terms_plan.plan(con, D("2026-10-20"), ["D1"])
    assert [t["need"] for t in p["trades"]] == [False] and p["have"] == ["1001"]


def test_the_plan_keys_a_leg_the_way_the_link_does() -> None:
    assert dv.trade_key({"book_id": "B1", "trade_id": 7}) == ("B1", "7")
    legs = [{"trade_id": "T1", "book_id": "B1", "product_kind": "fx_forward"},
            {"trade_id": "T1", "book_id": "B2", "product_kind": "fx_forward"}]
    linked, _, _ = dv.link([("P", "B2", {"deal_id": "T1"})], legs)
    assert [dv.trade_key(t) for t in linked["P"]] == [("B2", "T1")]  # the link takes the book's own leg only
