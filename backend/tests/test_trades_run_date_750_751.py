"""TDM#750: the trades screens open on the date of the run the pages show, not on the latest date that has trades (on a
Monday that was Friday's, shown as the day's); a day without trades is empty and points at the last one that had them.
TDM#751: each trade's PnL names the valuation price it is measured against (``mark_pct``, clean, % of the face, and
``mark_origin``) in the stored trades, the API rows and the card's trade data. Synthetic data only."""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_trades_585 import DS, _fresh_process, _settings  # noqa: F401 - fixture

from desk import store, trades
from desk.api import concepts, create_app
from desk.inbox import scan
from desk.pnl import new_trades as nt
from desk.valuation import positions as pv1

D = date.fromisoformat


def _trades_of_the_20th(con, tmp_path: Path) -> None:
    inbox = tmp_path / "in"
    inbox.mkdir()
    for n in ("trades_20261020_1100.csv", "trades_20261020_1500.csv"):
        shutil.copy(DS / n, inbox / n)
    assert scan.scan(con, _settings(tmp_path, inbox)).ok


def _run(con, day: str, *, run_type: str = "EOD", cut: str = "eod_final", lineage: dict | None = None,
         position: dict | None = None) -> int:
    """A complete run of ``day`` (a close, else the type and cut asked for) with, when given, one position holding
    ``position`` as its PnL."""
    rid = con.execute(
        "INSERT INTO run(run_type,business_date,cut_type,status,market_snapshot_id,static_snapshot_id,"
        "private_inputs_hash,position_set_ids,positions_known_at,core_version,backend_version,lineage)"
        " VALUES (%s,%s,%s,'loading','m','s','h','{}',now(),'c','b',%s) RETURNING run_id",
        (run_type, day, cut, Jsonb(lineage or {}))).fetchone()[0]
    if position is not None:
        con.execute("INSERT INTO run_position(run_id,position_id,ord,book_id,secid,quantity,pnl) VALUES"
                    " (%s,'T1|SEC-26238',1,'T1','SEC-26238',3200,%s)", (rid, Jsonb(position)))
    con.execute("UPDATE run SET status = 'complete', completed_at = now() WHERE run_id = %s", (rid,))
    con.commit()
    return rid


# ---------- TDM#750: the date of the run, not the latest date with trades ----------

def test_without_a_date_the_screen_is_the_runs_day_empty_with_a_pointer_to_the_last_day_with_trades(store_url, tmp_path):
    with store.connect(store_url) as con:
        _trades_of_the_20th(con, tmp_path)   # only the trades of D-1 (20.10) are stored
        _run(con, "2026-10-20")
        rid = _run(con, "2026-10-21")         # the latest run: D
        v, b = trades.view(con), trades.brief(con)
        by_run = trades.view(con, None, run_id=rid)
        explicit = trades.view(con, D("2026-10-20"))
        legacy_day = trades.default_date(con)
    assert legacy_day == D("2026-10-21")
    assert v["date"] == "2026-10-21" and v["rows"] == [] and v["summary"]["trades"] == 0
    assert v["earlier"] == {"date": "2026-10-20", "trades": 5}   # the pointer; the list is not filled with D-1
    assert b["date"] == "2026-10-21" and b["trades"] == 0 and b["earlier"] == {"date": "2026-10-20", "trades": 5}
    assert by_run["date"] == "2026-10-21" and by_run["earlier"]["date"] == "2026-10-20"
    assert explicit["date"] == "2026-10-20" and len(explicit["rows"]) == 5 and explicit["earlier"] is None


def test_the_api_without_a_date_and_with_one(store_url, tmp_path):
    with store.connect(store_url) as con:
        _trades_of_the_20th(con, tmp_path)
        _run(con, "2026-10-21")
    c = TestClient(create_app(store_url))
    v = c.get("/api/v1/trades").json()
    assert v["date"] == "2026-10-21" and v["rows"] == [] and v["earlier"] == {"date": "2026-10-20", "trades": 5}
    assert c.get("/api/v1/trades/summary").json()["earlier"]["date"] == "2026-10-20"
    kept = c.get("/api/v1/trades", params={"date": "2026-10-20"}).json()   # an asked date is as it was
    assert kept["date"] == "2026-10-20" and kept["summary"]["trades"] == 5 and kept["earlier"] is None


def test_the_pointer_skips_a_day_closed_by_a_later_empty_full_snapshot(store_url, tmp_path):
    """The 20th had trades, then a later full snapshot of it came with none (all closed): its older sets have rows, its
    current set has not. The pointer of the 21st is the 19th, whose trades stand."""
    inbox = tmp_path / "in"
    inbox.mkdir()
    for n in ("trades_20261020_1100.csv", "trades_20261020_1500.csv"):
        shutil.copy(DS / n, inbox / n)
    head, *lines = (DS / "trades_20261020_1500.csv").read_text(encoding="utf-8-sig").splitlines()
    (inbox / "trades_20261019_1500.csv").write_text(
        "\n".join([head, *(ln.replace("2026-10-20", "2026-10-19").replace("2026-10-21", "2026-10-20") for ln in lines[:2])]) + "\n",
        encoding="utf-8")
    with store.connect(store_url) as con:
        assert scan.scan(con, _settings(tmp_path, inbox)).ok
        assert len(store.dataset_rows(con, "trades", D("2026-10-20")).rows) == 5
        # a later full snapshot of every unit of the 20th with no trade in it (the units' current sets have no rows)
        for scope, src, prio, rd, ts, sha in con.execute(
                "SELECT DISTINCT ON (scope_key) scope, source, priority, report_date, report_ts, inbox_file_sha256 FROM dataset_set"
                " WHERE dataset = 'trades' AND valid_date = '2026-10-20' ORDER BY scope_key, report_ts DESC").fetchall():
            store.store_dataset_set(con, dataset="trades", scope=scope, valid_date=D("2026-10-20"), rows=[], source=src,
                                    priority=prio, report_date=rd, report_ts=ts + timedelta(hours=2), seq=None,
                                    generated_at=None, file_mtime=ts, file_name="empty.csv", sha256=sha,
                                    adapter_fingerprint="t", unit_hash="empty-" + str(scope), completeness="full_snapshot")
        con.commit()
        assert store.dataset_rows(con, "trades", D("2026-10-20")).rows == []   # closed
        assert con.execute("SELECT count(*) FROM dataset_set WHERE dataset = 'trades' AND valid_date = '2026-10-20'"
                           " AND row_count > 0").fetchone()[0] > 0               # but its older sets have rows
        found = trades.earlier_trades(con, D("2026-10-21"))
        assert found is not None and found["date"] == "2026-10-19" and found["trades"] > 0
        assert trades.earlier_trades(con, D("2026-10-19")) is None


def test_a_day_with_trades_is_opened_as_it_is_and_without_a_run_the_latest_trade_date_stays(store_url, tmp_path):
    with store.connect(store_url) as con:
        _trades_of_the_20th(con, tmp_path)
        assert trades.view(con)["date"] == "2026-10-20"   # no run at all: nothing to take the date from
        _run(con, "2026-10-20")
        assert trades.view(con)["date"] == "2026-10-20" and trades.view(con)["earlier"] is None
        _run(con, "2026-10-22", run_type="TEST")           # a test run does not give the date of the pages
        assert trades.view(con)["date"] == "2026-10-20"


# ---------- TDM#751: the valuation price of a trade ----------

def _ctx():
    return SimpleNamespace(static=SimpleNamespace(bonds={"S": SimpleNamespace(
        face_unit=None, currency="RUB", initial_face_value=1000.0)}), business_date=D("2026-10-16"))


TERMS = SimpleNamespace(face_unit=None, currency="RUB")


def _t(**kw):
    return {"trade_id": "T", "trade_version": 1, "side": "buy", "quantity": "100", "price_pct": "99.5", "ccy": "RUB",
            "accrued_ccy": "1000", **kw}


@pytest.fixture
def stub_face(monkeypatch):
    monkeypatch.setattr(pv1, "face_on", lambda t, d: 1000.0)


def test_a_purchase_below_the_mark_has_the_mark_and_its_pnl_is_the_difference_of_the_prices(stub_face):
    ref = SimpleNamespace(dirty_rub=1010.0, accrued_rub=10.0, face_rub=1000.0, clean_pct=100.0, price_basis="model")
    r = nt.trade_pnl(_ctx(), "S", TERMS, ref, _t())
    assert r["mark_pct"] == pytest.approx(100.00) and r["mark_origin"] == "model"
    assert r["pnl_rub"] == pytest.approx(100 * 1000 * 0.5 / 100)   # face x (mark - price) %, the accrued the same
    sold = nt.trade_pnl(_ctx(), "S", TERMS, ref, _t(side="sell"))
    assert sold["mark_pct"] == pytest.approx(100.0) and sold["pnl_rub"] == pytest.approx(-500.0)


def test_the_mark_is_the_clean_price_of_the_valuation_whatever_it_is_called(stub_face):
    ref = SimpleNamespace(dirty_rub=650.0, accrued_rub=10.0, face_rub=1000.0)   # no clean price, no basis: derived
    r = nt.trade_pnl(_ctx(), "S", TERMS, ref, _t())
    assert r["mark_pct"] == pytest.approx(64.0) and r["mark_origin"] is None
    # the owner's words: model / intraday cut / close / report (the day's average stands in for a missing close)
    for basis, origin in (("close", "close"), ("legal_close", "close"), ("waprice", "close"), ("last", "intraday"),
                          ("model", "model"), ("par", "par"), ("reported", "report"), ("new_basis", None)):
        got = nt.trade_pnl(_ctx(), "S", TERMS, SimpleNamespace(dirty_rub=650.0, accrued_rub=10.0, face_rub=1000.0,
                                                               clean_pct=64.0, price_basis=basis), _t())
        assert got["mark_origin"] == origin and got["mark_pct"] == pytest.approx(64.0) and got["mark_date"] is None


def test_a_stale_price_is_its_own_origin_with_its_date_when_the_valuation_says_it(stub_face):
    def ref(*notes):
        return SimpleNamespace(dirty_rub=650.0, accrued_rub=10.0, face_rub=1000.0, clean_pct=64.0, price_basis="close",
                               quality="stale_price", assumptions=notes)
    dated = nt.trade_pnl(_ctx(), "S", TERMS, ref("last price 2026-10-13, 3 days old (close)"), _t())
    assert (dated["mark_origin"], dated["mark_date"]) == ("stale", "2026-10-13")
    undated = nt.trade_pnl(_ctx(), "S", TERMS, ref("something else"), _t())
    assert (undated["mark_origin"], undated["mark_date"]) == ("stale", None)
    fresh = SimpleNamespace(dirty_rub=650.0, accrued_rub=10.0, face_rub=1000.0, clean_pct=64.0, price_basis="close",
                            quality="market_value", assumptions=())
    assert nt.trade_pnl(_ctx(), "S", TERMS, fresh, _t())["mark_origin"] == "close"


def test_the_date_of_a_stale_price_is_read_from_the_text_the_ladder_writes(stub_face):
    """The assumption is the one ``valuation.fallback`` writes (``stale_note``, the function both of its rungs use), not a
    copy of its words: ``mark_of`` reads the date back out of it."""
    from desk.valuation import fallback

    price_day, bd = D("2026-10-13"), D("2026-10-16")
    note = fallback.stale_note(price_day, bd, "close")
    assert "2026-10-13" in note and "3 days" in note
    ref = SimpleNamespace(dirty_rub=650.0, accrued_rub=10.0, face_rub=1000.0, clean_pct=64.0, price_basis="close",
                          quality="stale_price", assumptions=("something else", note))
    got = nt.trade_pnl(_ctx(), "S", TERMS, ref, _t())
    assert (got["mark_origin"], got["mark_date"]) == ("stale", "2026-10-13")


def test_a_trade_without_a_valuation_or_a_price_of_its_own(stub_face):
    ref = SimpleNamespace(dirty_rub=1010.0, accrued_rub=10.0, face_rub=1000.0, clean_pct=100.0, price_basis="close")
    none = nt.trade_pnl(_ctx(), "S", TERMS, None, _t())
    assert none["flag"] == "no_valuation" and none["mark_pct"] is None and none["mark_origin"] is None
    bad = nt.trade_pnl(_ctx(), "S", TERMS, SimpleNamespace(dirty_rub=float("nan"), accrued_rub=10.0, face_rub=1000.0,
                                                           clean_pct=100.0, price_basis="close"), _t())
    assert bad["flag"] == "no_valuation" and bad["mark_pct"] is None
    unpriced = nt.trade_pnl(_ctx(), "S", TERMS, ref, _t(price_pct=None))   # the mark is the valuation's, not the trade's
    assert unpriced["flag"] == "no_trade_price" and unpriced["mark_pct"] == pytest.approx(100.0)
    assert nt.trade_pnl(_ctx(), "S", TERMS, ref, _t(ccy="USD"))["mark_pct"] is None


def test_the_mark_is_in_the_api_rows_and_an_older_run_has_none(store_url, tmp_path):
    new = {"trade_id": "TR-10", "side": "buy", "quantity": 1200, "price_pct": 99.5, "pnl_rub": 6000.0, "flag": None,
           "mark_pct": 100.0, "mark_origin": "stale", "mark_date": "2026-10-13"}
    old = {"trade_id": "TR-10", "side": "buy", "quantity": 1200, "price_pct": 99.5, "pnl_rub": 6000.0, "flag": None}
    lineage = {"intraday_trades": {"applied": 1, "versions": {"TR-10": 2}, "skipped_ids": {}}}
    with store.connect(store_url) as con:
        _trades_of_the_20th(con, tmp_path)
        r_new = _run(con, "2026-10-20", lineage=lineage, position={"extra": {"trades": [new], "start_quantity": 2000}})
        r_old = _run(con, "2026-10-20", run_type="INTRADAY", cut="intraday", lineage=lineage,
                     position={"extra": {"trades": [old], "start_quantity": 2000}})
        v_new = trades.view(con, D("2026-10-20"), run_id=r_new)
        v_old = trades.view(con, D("2026-10-20"), run_id=r_old)
    row = next(r for r in v_new["rows"] if r["trade_id"] == "TR-10")
    assert (row["mark_pct"], row["mark_origin"], row["mark_date"], row["pnl_rub"]) == (100.0, "stale", "2026-10-13", 6000.0)
    assert all(r["mark_pct"] is None and r["mark_origin"] is None for r in v_new["rows"] if r["trade_id"] != "TR-10")
    old_row = next(r for r in v_old["rows"] if r["trade_id"] == "TR-10")
    assert old_row["pnl_rub"] == 6000.0 and old_row["mark_pct"] is None and old_row["mark_origin"] is None
    assert old_row["mark_date"] is None


def test_the_cards_trade_data_has_the_mark_of_each_side_and_the_position():
    rows = [{"side": "buy", "quantity": 3.0, "price_pct": 99.0, "mark_pct": 100.0, "mark_origin": "model"},
            {"side": "buy", "quantity": 1.0, "price_pct": 99.5, "mark_pct": 100.0, "mark_origin": "model"},
            {"side": "sell", "quantity": 2.0, "price_pct": 101.0, "mark_pct": 100.0, "mark_origin": "model"}]
    t = concepts.trade_of({"trades": rows, "quantity": 10.0, "start": 8.0}, [10000.0, 0, 0, 0, 0, 0, 99.0], D("2026-10-16"))
    assert t["buy"]["mark"] == pytest.approx(100.0) and t["buy"]["mark_origin"] == "model"
    assert t["sell"]["mark"] == pytest.approx(100.0) and t["mark"] == pytest.approx(100.0) and t["mark_origin"] == "model"
    assert t["date"] == "2026-10-16"   # TDM#750: the card names the day of the run whose trades they are
    stale = concepts.trade_of({"trades": [{"side": "buy", "quantity": 1.0, "mark_pct": 99.0, "mark_origin": "stale",
                                           "mark_date": "2026-10-13"}], "quantity": 5.0, "start": 4.0}, [5000.0, 0, 0, 0, 0, 0, 99.0])
    assert stale["mark_origin"] == "stale" and stale["mark_date"] == "2026-10-13" and stale["buy"]["mark_date"] == "2026-10-13"
    assert t["mark_date"] is None
    # a run before TDM#751 has no mark: none, not zero
    old = concepts.trade_of({"trades": [{"side": "buy", "quantity": 1.0, "price_pct": 99.0}], "quantity": 5.0,
                             "start": 4.0}, [5000.0, 0, 0, 0, 0, 0, 99.0])
    assert old["mark"] is None and old["buy"]["mark"] is None and old["buy"]["mark_origin"] is None and "date" not in old
    # trades valued differently: the average by the quantity, the origin says so
    mixed = concepts.trade_of({"trades": [{"side": "buy", "quantity": 1.0, "mark_pct": 100.0, "mark_origin": "model"},
                                          {"side": "buy", "quantity": 3.0, "mark_pct": 99.0, "mark_origin": "market"}],
                               "quantity": 5.0, "start": 1.0}, [5000.0, 0, 0, 0, 0, 0, 99.0])
    assert mixed["mark"] == pytest.approx(99.25) and mixed["mark_origin"] == "mixed"
