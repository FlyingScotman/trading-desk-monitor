"""TDM#468 (+ TDM#503): the switch «Деривативы вне отчёта позиций» (``off_report_derivatives``: exclude | include).

On the hub's contract 1.4 golden snapshot of 15.10.2026 and synthetic trades (invented numbers):

- the setting: ``exclude`` by default, the old flag ``fx_unlinked_terms`` means ``include`` until the new key is set;
- the selection (``valuation.offreport``): FX, IRS / OIS, CCS and REPO trades no positions row names, also of books
  without a positions set; the trade that looks like one the report has (``suspect``) and the unreadable one are
  not included; a trade that matured long ago is only counted;
- the plan values the included ones from their terms as rows of their own, marked ``off_report``;
- ``private_inputs_hash`` says the mode only when it matters;
- the PnL: the same trade on both days, a trade that moved into the report, a new trade, the last day of a closed one;
- the OCP: a trade of the ``trades`` dataset the run holds as an off-report row is not counted twice; the gap of
  the excluded ones.
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from test_deriv_pnl_flows_466 import _with_fixings_from_september, repo, swap
from test_ocp_base_plus_day import BOOKS as OCP_BOOKS
from test_ocp_base_plus_day import POLICY as OCP_POLICY
from test_slice_s2 import shifted_prev

from desk.api import settings_routes
from desk.curves import fx as fxcurves
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.pnl import direct
from desk.pnl.reasons import NEW_TRADE_METHOD, NO_PNL_TEXT
from desk.publish import desk_run, ocp_run, tdm_run
from desk.rules import ocp as O
from desk.settings import SettingsError, load, update_settings_json
from desk.store import PositionInputs
from desk.valuation import offreport
from desk.valuation import positions as pv1

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1_4"
BD = date(2026, 10, 15)
PREV = date(2026, 10, 14)
B = date(2026, 10, 15)  # the OCP report's date (test_ocp_base_plus_day)
D = date(2026, 10, 16)


@pytest.fixture(scope="module")
def ctx0():
    return _with_fixings_from_september(prepare(SnapshotDir(FIX), BD.isoformat()))


def on(ctx, include: bool):
    return replace(ctx, fx_options=fxcurves.FxOptions(off_report=include))


@pytest.fixture(scope="module")
def prev0(ctx0):
    import copy

    p = shifted_prev(ctx0, PREV, 0.0)
    m = copy.copy(ctx0.market)
    m.business_date = PREV
    m.fx = ctx0.market.fx
    return dataclasses.replace(p, market=m)


# --- the setting ------------------------------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    import os

    for v in [v for v in os.environ if v.startswith("DESK_")]:
        monkeypatch.delenv(v)
    monkeypatch.setenv("DESK_HOME", str(tmp_path))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    return tmp_path


def test_setting_defaults_to_exclude_and_the_old_flag_means_include(home):
    assert load().off_report_derivatives == "exclude"
    update_settings_json({"fx_unlinked_terms": True}, home)
    assert load().off_report_derivatives == "include"  # the old flag until the new key is set
    assert fxcurves.options_from(load()).off_report is True
    update_settings_json({"off_report_derivatives": "exclude"}, home)  # the new key wins; the old one is dropped
    s = load()
    assert s.off_report_derivatives == "exclude" and not hasattr(s, "fx_unlinked_terms")
    assert "fx_unlinked_terms" not in settings_routes.settings_payload()
    update_settings_json({"off_report_derivatives": "include"}, home)
    assert load().off_report_derivatives == "include"
    update_settings_json({"off_report_derivatives": None}, home)  # removing the key: the default
    assert load().off_report_derivatives == "exclude"
    assert ("off_report_derivatives", "exclude") in load().describe()


def test_put_with_the_new_key_removes_the_old_one_and_get_shows_include(home):
    update_settings_json({"fx_unlinked_terms": True}, home)
    assert settings_routes.settings_payload()["off_report_derivatives"] == "include"  # what the list shows
    update_settings_json({"off_report_derivatives": None}, home)  # saved from the page as «по умолчанию»
    doc = settings_routes.settings_payload()
    assert "fx_unlinked_terms" not in doc and "off_report_derivatives" not in doc
    assert load().off_report_derivatives == "exclude"
    with pytest.raises(SettingsError):
        update_settings_json({"off_report_derivatives": "maybe"}, home)
    assert load().off_report_derivatives == "exclude"  # nothing written on an error


def test_environment_overrides_and_the_legacy_variable(home):
    assert load({"DESK_FX_UNLINKED_TERMS": "1", "DESK_ENV_FILE": str(home / "n.env"),
                 "DESK_HOME": str(home)}).off_report_derivatives == "include"
    assert load({"DESK_OFF_REPORT_DERIVATIVES": "exclude", "DESK_FX_UNLINKED_TERMS": "1",
                 "DESK_ENV_FILE": str(home / "n.env"), "DESK_HOME": str(home)}).off_report_derivatives == "exclude"


# --- the selection ----------------------------------------------------------------------------------------------


def fxt(tid, vd="2026-12-17", book="FXB", trade_date="2026-10-15", **kw):
    return {"trade_id": tid, "book_id": book, "product_kind": "fx_forward", "trade_date": trade_date, "value_date": vd,
            "base_ccy": "CNY", "quote_ccy": "RUB", "side": "buy", "notional_base": "10000000",
            "notional_quote": "127460000", "contract_rate": "12.746", **kw}


def ccs(tid, book="IRB", **kw):
    base = {"trade_id": tid, "book_id": book, "product_kind": "ccs", "trade_date": "2026-10-05",
            "start_date": "2026-10-06", "maturity_date": "2028-10-06", "payment_frequency": "3M"}
    return [{**base, "leg_id": "rub", "side": "pay", "ccy": "RUB", "notional_ccy": "1000000000",
             "fixed_rate_pct": "15", **kw},
            {**base, "leg_id": "usd", "side": "receive", "ccy": "USD", "notional_ccy": "12000000",
             "fixed_rate_pct": "5", **kw}]


def mk(rows=(), terms=(), other=(), on_date=BD):
    """Inputs: rows = (position_id, book, row); books of rows have a set; ``other``: terms of books without one."""
    rows = list(rows)
    return PositionInputs([(pid, b, None, None) for pid, b, _ in rows], [], {b: on_date for _, b, _ in rows},
                          datetime.now(UTC), "0" * 64, {pid: x for pid, _, x in rows}, trade_terms=list(terms),
                          other_book_terms=list(other))


LINKED_ROW = ("FXB|L1", "FXB", {"asset_class": "fx", "ccy": "CNY", "amount_ccy": 1e6, "value_date": date(2026, 12, 17),
                                "deal_id": "L1"})
TERMS = [fxt("L1", vd="2026-12-17"), fxt("N1", vd="2027-01-20"), *swap("S1", book_id="IRB"),
         repo("R1", "repo", "2026-10-14", "2026-11-16", fixed_rate_pct="16", book_id="IRB"), *ccs("C1"),
         fxt("M1", vd="2026-09-01")]  # M1 matured long ago


def by_key(trades):
    return {t.key: t for t in trades}


def test_selection_by_mode_and_kind():
    inp = mk([LINKED_ROW], TERMS)
    ex, matured = offreport.off_report_trades(inp, BD, False)
    assert matured == 1 and list(by_key(ex)) == ["FXB|N1", "IRB|C1", "IRB|R1", "IRB|S1"]
    assert {t.state for t in ex} == {"excluded"}
    assert {t.key: t.kind for t in ex} == {"FXB|N1": "fx_forward", "IRB|C1": "ccs", "IRB|R1": "repo", "IRB|S1": "ois"}
    inc, _ = offreport.off_report_trades(inp, BD, True)
    assert {t.state for t in inc} == {"included"} and by_key(inc)["FXB|N1"].last == date(2027, 1, 20)
    assert by_key(inc)["FXB|N1"].trade_date == BD
    assert all(t.key != "FXB|L1" for t in inc)  # the linked trade is the report's


def test_a_book_without_a_positions_set_counts_while_the_choice_holds(monkeypatch):
    other = [fxt("O1", book="NOB")]
    inp = mk([LINKED_ROW], [fxt("L1")], other=other)
    got = by_key(offreport.off_report_trades(inp, BD, True)[0])
    assert got["NOB|O1"].state == "included" and got["NOB|O1"].other_book
    monkeypatch.setattr(offreport, "BOOKS_WITHOUT_REPORT", False)  # question 3, answer (b)
    assert "NOB|O1" not in by_key(offreport.off_report_trades(inp, BD, True)[0])


def test_a_trade_that_looks_like_one_of_the_report_is_not_included(monkeypatch):
    rows = [LINKED_ROW,
            ("OTH|P9", "OTH", {"asset_class": "fx", "ccy": "CNY", "amount_ccy": 1.0, "deal_id": "D7"}),
            # a row without terms of the same kind, currency and value date in the trade's book
            ("FXB|NOTERMS", "FXB", {"asset_class": "fx", "ccy": "CNY", "amount_ccy": 10e6,
                                    "value_date": date(2027, 1, 20)}),
            # same kind, currency and date, another amount: another deal
            ("FXB|OTHER", "FXB", {"asset_class": "fx", "ccy": "CNY", "amount_ccy": 3e6,
                                  "value_date": date(2027, 3, 17)})]
    terms = [fxt("L1"), fxt("D7"), fxt("N1", vd="2027-01-20"), fxt("N2", vd="2027-03-17")]
    got = by_key(offreport.off_report_trades(mk(rows, terms), BD, True)[0])
    assert got["FXB|D7"].state == "suspect" and "another book" in got["FXB|D7"].why
    assert got["FXB|N1"].state == "suspect" and "NOTERMS" in got["FXB|N1"].why
    assert got["FXB|N2"].state == "included"  # the row of 27.03 is for 3m CNY, not N2's 10m
    own = by_key(offreport.off_report_trades(mk([LINKED_ROW, ("FXB|X", "FXB", {"asset_class": "bond", "deal_id": "N3"})],
                                               [fxt("L1"), fxt("N3", vd="2027-05-17")]), BD, True)[0])
    assert own["FXB|N3"].state == "suspect" and "this book" in own["FXB|N3"].why  # a row of any class, any book
    monkeypatch.setattr(offreport, "SUSPECT_ARE_INCLUDED", True)  # question 2, answer (b)
    assert by_key(offreport.off_report_trades(mk(rows, terms), BD, True)[0])["FXB|D7"].state == "included"


def test_unreadable_terms_are_invalid_not_dropped():
    bad = fxt("B1")
    bad.pop("notional_quote")
    got = by_key(offreport.off_report_trades(mk([LINKED_ROW], [fxt("L1"), bad]), BD, True)[0])
    assert got["FXB|B1"].state == "invalid" and got["FXB|B1"].why.startswith("terms_incomplete")
    s = offreport.summary(list(got.values()), 0, "include")
    assert s["invalid"] == 1 and s["included"] == 0 and s["list"][0]["state"] == "invalid"


def test_summary_counts_and_bounds_the_list():
    trades = [offreport.OffReportTrade("B", f"T{i}", ("fx_forward",), BD, BD, "excluded") for i in range(250)]
    s = offreport.summary(trades, 3, "exclude")
    assert (s["trades"], s["matured"], s["excluded"], s["books"], s["by_kind"]) == (
        250, 3, 250, {"B": 250}, {"fx_forward": 250})
    assert len(s["list"]) == offreport.LIST_MAX and s["list_truncated"] == 250


# --- the plan ---------------------------------------------------------------------------------------------------


def test_exclude_values_nothing_extra_include_values_each_trade(ctx0):
    inp = mk([LINKED_ROW], [fxt("L1"), fxt("N1", vd="2027-01-20"), *swap("S1", book_id="IRB"),
                            repo("R1", "repo", "2026-10-14", "2026-11-16", fixed_rate_pct="16", book_id="IRB"),
                            *ccs("C1")], other=[fxt("O1", book="NOB")])
    base = pv1.plan(on(ctx0, False), inp)
    assert base.added == [] and "FXB|N1" not in base.direct and len(base.off_report) == 5
    assert {t.state for t in base.off_report} == {"excluded"}
    inp = mk([LINKED_ROW], [fxt("L1"), fxt("N1", vd="2027-01-20"), *swap("S1", book_id="IRB"),
                            repo("R1", "repo", "2026-10-14", "2026-11-16", fixed_rate_pct="16", book_id="IRB"),
                            *ccs("C1")], other=[fxt("O1", book="NOB")])
    inp = replace(inp, other_books={"NOB": {"book_id": "NOB", "name": "No report"}},
                  other_book_units=[{"scope": {"book_id": "NOB"}, "set_id": 1}])
    p = pv1.plan(on(ctx0, True), inp)
    assert sorted(p.added) == ["FXB|N1", "IRB|C1", "IRB|R1", "IRB|S1", "NOB|O1"]
    for pid in ("FXB|N1", "IRB|S1", "IRB|R1", "NOB|O1"):
        o = p.direct[pid]
        assert o.valuation is not None and o.valuation.extra["core"]["trades"] == [pid.split("|")[1]]
        assert inp.extra[pid]["off_report"] is True
    assert p.direct["FXB|N1"].valuation.extra["core"]["product"] == "fx_forward"
    assert p.direct["IRB|S1"].valuation.extra["core"]["product"] == "ois"
    assert p.direct["IRB|R1"].valuation.extra["core"]["product"] == "repo"
    assert "IRB|C1" in p.direct  # a CCS is a row of its own too (valued, or with core's reason)
    assert [pid for pid, _ in p.order][-5:] == p.added and inp.books["NOB"]["name"] == "No report"
    assert inp.trade_terms_units == [{"scope": {"book_id": "NOB"}, "set_id": 1}]
    # the linked row is valued as before
    assert p.direct["FXB|L1"].valuation.pv_rub == base.direct["FXB|L1"].valuation.pv_rub


def test_the_published_row_says_where_it_comes_from(ctx0):
    inp = mk([LINKED_ROW], [fxt("L1"), fxt("N1", vd="2027-01-20"), fxt("N2", vd="2027-03-17", trade_date="2026-10-01")])
    p = pv1.plan(on(ctx0, True), inp)
    new = tdm_run.direct_row(ctx0, "FXB", "FXB|N1", p.direct["FXB|N1"], inp.extra["FXB|N1"], None)
    old = tdm_run.direct_row(ctx0, "FXB", "FXB|N2", p.direct["FXB|N2"], inp.extra["FXB|N2"], None)
    lnk = tdm_run.direct_row(ctx0, "FXB", "FXB|L1", p.direct["FXB|L1"], inp.extra["FXB|L1"], None)
    assert new["extra"]["off_report"] is True and new["new_today"] is True  # made on the run's date
    assert old["extra"]["off_report"] is True and old["new_today"] is False
    assert "off_report" not in lnk["extra"] and lnk["new_today"] is False
    assert new["name"].startswith("CNY/RUB") and new["extra"]["valuation"] == "computed"


def test_plan_twice_on_the_same_inputs_is_stable(ctx0):
    inp = mk([LINKED_ROW], [fxt("L1"), fxt("N1", vd="2027-01-20")])
    a = pv1.plan(on(ctx0, True), inp)
    b = pv1.plan(on(ctx0, True), inp)
    assert a.added == b.added == ["FXB|N1"] and len(b.order) == 2


# --- the inputs hash --------------------------------------------------------------------------------------------


def test_the_hash_changes_only_with_include_and_such_trades():
    inp = mk([LINKED_ROW], [fxt("L1"), fxt("N1", vd="2027-01-20")])
    assert offreport.stamp(inp, BD, "exclude") is inp  # exclude: as before
    inc = offreport.stamp(inp, BD, "include")
    assert inc.private_inputs_hash != inp.private_inputs_hash
    assert offreport.stamp(inp, BD, "include").private_inputs_hash == inc.private_inputs_hash  # stable
    none = mk([LINKED_ROW], [fxt("L1")])
    assert offreport.stamp(none, BD, "include") is none  # include, nothing off the report: as before
    gone = mk([LINKED_ROW], [fxt("L1"), fxt("M1", vd="2026-09-01")])
    assert offreport.stamp(gone, BD, "include") is gone  # matured long ago: nothing to say
    # the terms of a book without a set are in the hash of the included ones, not of the excluded
    o1 = mk([LINKED_ROW], [fxt("L1")], other=[fxt("O1", book="NOB")])
    o2 = mk([LINKED_ROW], [fxt("L1")], other=[fxt("O1", book="NOB", notional_base="9000000")])
    assert offreport.stamp(o1, BD, "include").private_inputs_hash != offreport.stamp(o2, BD, "include").private_inputs_hash
    assert offreport.stamp(o1, BD, "exclude").private_inputs_hash == o1.private_inputs_hash
    # a suspect trade counts too: leaving the mode out of the hash would hide a change of what is held back
    sus = mk([LINKED_ROW, ("OTH|P", "OTH", {"asset_class": "fx", "ccy": "CNY", "deal_id": "D7"})],
             [fxt("L1"), fxt("D7")])
    assert offreport.stamp(sus, BD, "include") is not sus


def test_the_hash_of_a_day_covers_the_trades_included_on_the_day_before():
    """A trade off the report on D-1 and in the report on D: nothing is included on D, yet the PnL of D depends on the
    mode (D-1 values the trade as a row of its own), so switching changes the hash of D."""
    on_d = mk([LINKED_ROW, ("FXB|N1", "FXB", {"asset_class": "fx", "ccy": "CNY", "amount_ccy": 1e7,
                                              "value_date": date(2027, 1, 20), "deal_id": "N1"})],
              [fxt("L1"), fxt("N1", vd="2027-01-20")])
    on_prev = mk([LINKED_ROW], [fxt("L1"), fxt("N1", vd="2027-01-20")], on_date=PREV)
    assert offreport.stamp(on_d, BD, "include") is on_d  # without D-1: nothing to say
    stamped = offreport.stamp(on_d, BD, "include", on_prev, PREV)
    assert stamped.private_inputs_hash != on_d.private_inputs_hash
    assert offreport.stamp(on_d, BD, "exclude", on_prev, PREV) is on_d  # exclude: as before
    nothing = mk([LINKED_ROW], [fxt("L1")], on_date=PREV)
    assert offreport.stamp(on_d, BD, "include", nothing, PREV) is on_d


def test_intraday_inputs_goes_through_the_stamp(monkeypatch):
    from types import SimpleNamespace

    from desk import trades

    inp = mk([LINKED_ROW], [fxt("L1"), fxt("N1", vd="2027-01-20")])
    monkeypatch.setattr(trades.store, "positions_for", lambda con, day, **kw: inp)
    off = SimpleNamespace(intraday_trades=False, off_report_derivatives="exclude")
    on_ = SimpleNamespace(intraday_trades=False, off_report_derivatives="include")
    h0 = trades.intraday_inputs(None, BD, off)[0].private_inputs_hash
    h1 = trades.intraday_inputs(None, BD, on_)[0].private_inputs_hash
    assert h0 == inp.private_inputs_hash and h1 != h0


# --- the PnL ----------------------------------------------------------------------------------------------------


def two_days(ctx0, prev0, d_terms, p_terms, d_rows=(), p_rows=(), d_other=(), p_other=()):
    ctx, prev = on(ctx0, True), on(prev0, True)
    inp = mk(d_rows, d_terms, d_other, BD)
    pinp = mk(p_rows, p_terms, p_other, PREV)
    bp, pbp = pv1.plan(ctx, inp), pv1.plan(prev, pinp)
    direct.attach(prev, ctx, bp.direct, inp, pbp.direct, pinp.valid_dates, pinp)
    return bp, pbp


def test_pnl_of_the_same_off_report_trade_on_both_days(ctx0, prev0):
    t = swap("S1", book_id="IRB")
    bp, pbp = two_days(ctx0, prev0, t, t)
    o = bp.direct["IRB|S1"]
    assert o.no_pnl is None and o.pnl.method == "desk-fx-core-v1"
    assert o.pnl.total == pytest.approx(o.valuation.pv_rub - pbp.direct["IRB|S1"].valuation.pv_rub + o.pnl.received_rub)


def test_pnl_continues_when_the_trade_moves_into_the_report(ctx0, prev0):
    t = swap("S1", book_id="IRB")
    row = ("S1", "IRB", {"asset_class": "otc", "ccy": "RUB", "deal_id": "S1", "source_instrument_id": "S1"})
    bp, pbp = two_days(ctx0, prev0, t, t, d_rows=[row])  # D-1: off the report (no row); D: its row
    o = bp.direct["S1"]
    assert o.no_pnl is None and o.pnl.method == "desk-fx-core-v1"
    assert o.pnl.total == pytest.approx(o.valuation.pv_rub - pbp.direct["IRB|S1"].valuation.pv_rub + o.pnl.received_rub)
    # the other way: in the report on D-1, off it on D
    bp2, pbp2 = two_days(ctx0, prev0, t, t, p_rows=[row])
    o2 = bp2.direct["IRB|S1"]
    assert o2.no_pnl is None and o2.pnl.total == pytest.approx(
        o2.valuation.pv_rub - pbp2.direct["S1"].valuation.pv_rub + o2.pnl.received_rub)


def test_a_trade_made_today_has_its_pv_as_pnl_of_the_day(ctx0, prev0):
    new = swap("N9", book_id="IRB", trade_date="2026-10-15")
    bp, pbp = two_days(ctx0, prev0, new, [])
    o = bp.direct["IRB|N9"]
    assert "IRB|N9" not in pbp.direct and o.no_pnl is None
    assert o.pnl.method == NEW_TRADE_METHOD == "desk-core-new-trade-v1"
    assert o.pnl.new_trades == o.pnl.total == pytest.approx(o.valuation.pv_rub)
    assert o.pnl.explained == 0.0 and "новая сделка" in o.pnl.note
    assert o.pnl.as_tdm()["new_trades"] == o.pnl.new_trades
    # question 1, answer (b): no PnL, the reason says why
    direct.NEW_TRADE_PNL_FROM_PV = False
    try:
        bp, _ = two_days(ctx0, prev0, new, [])
        assert bp.direct["IRB|N9"].pnl is None and bp.direct["IRB|N9"].no_pnl.reason == "new_trade"
    finally:
        direct.NEW_TRADE_PNL_FROM_PV = True


def test_an_old_trade_missing_on_the_day_before_has_pnl_0_on_an_assumption(ctx0, prev0):
    """TDM#576: no terms of the trade on D-1: the PV of D stands for it, PnL 0, said on the row (not «нет PnL»)."""
    old = swap("S1", book_id="IRB")  # made 10.09, yet the previous day's inputs lack it
    bp, _ = two_days(ctx0, prev0, old, [])
    o = bp.direct["IRB|S1"]
    assert o.no_pnl is None and o.pnl.total == 0.0 and o.pnl.assumption == {"code": "start_pv_missing"}
    assert "no_prev_terms" in NO_PNL_TEXT   # the code stays for the runs stored before


def test_the_last_day_of_a_closed_trade_has_a_pnl_and_the_next_day_no_row(ctx0, prev0):
    ended = swap("E1", "2026-10-14", book_id="IRB")  # matured on D-1: its last flows are paid on D
    alive = swap("E1", "2027-09-14", book_id="IRB")
    bp, pbp = two_days(ctx0, prev0, ended, alive)
    o = bp.direct["IRB|E1"]
    assert o.valuation.pv_rub == 0.0 and o.valuation.extra["core"]["model_flags"] == ["settled"]
    assert o.no_pnl is None and o.pnl.total == pytest.approx(-pbp.direct["IRB|E1"].valuation.pv_rub + o.pnl.received_rub)
    long_ago = swap("E2", "2026-09-01", book_id="IRB")
    got, matured = offreport.off_report_trades(mk([], long_ago), BD, True)
    assert got == [] and matured == 1


# --- OCP ---------------------------------------------------------------------------------------------------------


def test_a_trade_held_as_an_off_report_row_is_not_counted_twice_in_the_ocp():
    trade = {"trade_id": "F1", "status": "live", "trade_date": "2026-10-16", "book_id": "D1", "position_id": "D1|X",
             "asset_class": "fx", "side": "sell", "amount_ccy": "100000", "ccy": "USD", "settle_date": "2026-12-16"}
    plain = O.trade_flows([trade], OCP_BOOKS, {}, {"D1|F1": "fx_forward"}, OCP_POLICY, after=date(2026, 10, 15))
    assert [(f.leg, f.pv_kind) for f in plain.flows] == [("fx", None)]
    rows = [{"position_id": "D1|F1", "book_id": "D1", "extra": {"off_report": True, "reported": {"deal_id": "F1"}}},
            {"position_id": "D1|Z", "book_id": "D1", "extra": {}}]
    held = O.off_report_held(rows)
    assert held == {("D1", "F1"): "D1|F1"}
    got = O.trade_flows([trade], OCP_BOOKS, {}, {"D1|F1": "fx_forward"}, OCP_POLICY, after=date(2026, 10, 15), held=held)
    assert [(f.leg, f.pv_kind, f.position_id) for f in got.flows] == [("fx", "fx_forward", "D1|F1")]


def test_gap_of_the_excluded_fx_trades_and_ccs():
    T = offreport.OffReportTrade
    summary = offreport.summary(
        [T("B1", "T1", ("fx_forward",), BD, BD, "excluded"), T("B2", "T2", ("ccs",), BD, BD, "excluded"),
         T("B2", "T3", ("ois",), BD, BD, "excluded"), T("B3", "T4", ("fx_forward",), BD, BD, "suspect"),
         T("B1", "T5", ("fx_forward",), PREV, BD, "closed")], 0, "exclude")
    (gap,) = ocp_run._off_report_gap(summary)
    assert gap["code"] == "off_report_excluded" and gap["items"] == ["B1 (1)", "B2 (1)"]  # not the OIS, suspect, closed
    assert "вынесен из базы ОВП" in gap["text"]
    assert ocp_run._off_report_gap({**summary, "mode": "include"}) == [] and ocp_run._off_report_gap(None) == []
    # the kind must be apart in the report of the book; a trade the trades dataset holds after the base is not a gap
    (only,) = ocp_run._off_report_gap(summary, {"B1": {"fx_forward"}, "B2": {"fx_forward"}})
    assert only["items"] == ["B1 (1)"]  # B2's CCS is a «swap» of the report, which B2 does not give apart
    assert ocp_run._off_report_gap(summary, {"B1": {"fx_forward"}}, {("B1", "T1")}) == []
    rows = [{"entity": "E1", "ccy": "USD", "derivatives_ccy": {"fx_forward": "5", "fx_future": None}},
            {"book_id": "B2", "entity": "E2", "ccy": "USD", "derivatives_ccy": {"swap": "1"}}]
    assert ocp_run._apart(rows) == {"entity:E1": {"fx_forward"}, "B2": {"swap"}}
    assert ocp_run._off_report_gap(summary, ocp_run._apart(rows), entity_of={"B1": "E1"})[0]["items"] == ["B1 (1)", "B2 (1)"]  # B2 gives swap apart
    # a report of scope ["entity"] has no book_id: its kinds are the entity's, the trade's book is taken to its entity
    assert ocp_run._off_report_gap(summary, {"entity:E1": {"fx_forward"}}, entity_of={"B1": "E1", "B2": "E2"})[0][
        "items"] == ["B1 (1)"]
    assert ocp_run._off_report_gap(summary, {"entity:E2": {"fx_forward"}}, entity_of={"B1": "E1"}) == []
    # counted from the whole list, not the one cut for the lineage
    many = offreport.summary([T("B", f"T{i}", ("fx_forward",), BD, BD, "excluded") for i in range(250)], 0, "exclude")
    assert len(many["list"]) == 200 and "(250)" in ocp_run._off_report_gap(many)[0]["items"][0]


def test_a_trade_that_ended_is_closed_not_a_gap_and_the_list_puts_the_gaps_first():
    ended = fxt("E1", vd="2026-10-10")  # its last day was five days ago
    got, matured = offreport.off_report_trades(mk([LINKED_ROW], [fxt("L1"), ended, fxt("N1", vd="2027-01-20")]), BD, False)
    assert matured == 0 and by_key(got)["FXB|E1"].state == "closed" and by_key(got)["FXB|N1"].state == "excluded"
    s = offreport.summary(got, matured, "exclude")
    assert (s["trades"], s["excluded"], s["closed"]) == (1, 1, 1) and [t["trade_id"] for t in s["list"]] == ["N1"]
    inc, _ = offreport.off_report_trades(mk([LINKED_ROW], [fxt("L1"), ended]), BD, True)
    assert by_key(inc)["FXB|E1"].state == "included"  # its last day has a PnL
    # cut at 200: the suspect, unreadable and excluded come before the included
    T = offreport.OffReportTrade
    big = [T("B", f"I{i}", ("fx_forward",), BD, BD, "included") for i in range(250)] + [
        T("B", "S", ("fx_forward",), BD, BD, "suspect"), T("B", "X", ("fx_forward",), BD, BD, "invalid")]
    assert [t["trade_id"] for t in offreport.summary(big, 0, "include")["list"][:2]] == ["S", "X"]


def test_the_ocp_counts_a_trade_held_as_an_off_report_row_once_whether_or_not_the_kind_is_apart(monkeypatch):
    """A forward after the base date, in the ``trades`` dataset and, with the switch ``include``, as a row of its own:
    it enters the published OCP once. Without the match (``held``) the apart kind counts it twice."""
    from test_ocp_base_plus_day import BOOKS, REPORT, ROWS, UNITS, core_row
    from test_ocp_base_plus_day import run as ocp

    trade = {"trade_id": "F9", "status": "live", "trade_date": "2026-10-16", "book_id": "D1", "position_id": "D1|X",
             "asset_class": "fx", "side": "buy", "amount_ccy": "200000", "ccy": "USD", "settle_date": "2026-12-16"}
    row = core_row("D1|F9", "D1", {"USD": 195_000.0}, nominal={"USD": 200_000.0})
    row["extra"].update(off_report=True, reported={"deal_id": "F9"})
    no_split = [{k: v for k, v in r.items() if k != "derivatives_ccy"} for r in REPORT]
    assert BOOKS and UNITS

    def usd(report, rows, trades):
        days = {B: O.BaseDay(B, run_id=1, trade_rows=trades)}
        r = ocp(on=D, report=report, rows=rows, days=days, valid_dates={"D1": D, "T1": D})
        return {(x["book_id"], x["ccy"]): x for x in r.published}[("D1", "USD")]["amount"]

    # kind apart: the row's PV replaces the trade's nominal; the amount equals the row alone
    assert usd(REPORT, [*ROWS, row], [trade]) == usd(REPORT, [*ROWS, row], []) == 825_000
    assert usd(REPORT, ROWS, [trade]) == 830_000  # the trade alone: its nominal
    monkeypatch.setattr(O, "off_report_held", lambda rows: {})
    assert usd(REPORT, [*ROWS, row], [trade]) == 1_025_000  # without the match: twice
    monkeypatch.undo()
    # kind not apart: the base holds the kinds' nominal; the trade is counted once, the row's PV is not added
    assert usd(no_split, [*ROWS, row], [trade]) == usd(no_split, ROWS, [trade]) == 340_000
    assert usd(no_split, [*ROWS, row], []) == 140_000


# --- lineage and the screens -------------------------------------------------------------------------------------


def test_old_runs_with_only_the_swap_count_are_still_read():
    from desk.api import coverage

    old = {"lineage": {"positions": {"unlinked_swap_terms": {"trades": 2, "books": {"IRB": 2}}}}}
    assert coverage.off_report(None, old) == {"mode": None, "legacy": True, "trades": 2, "books": {"IRB": 2}, "list": []}
    assert coverage.off_report(None, {"lineage": {"positions": {}}}) is None
    new = {"lineage": {"positions": {"off_report": offreport.summary(
        [offreport.OffReportTrade("B1", "T1", ("fx_forward",), BD, BD, "excluded"),
         offreport.OffReportTrade("B1", "T2", ("fx_forward",), BD, BD, "included")], 1, "include", 1)}}}
    got = coverage.off_report(None, new)
    assert got["mode"] == "include" and got["matured"] == 1 and [t["trade_id"] for t in got["list"]] == ["T1"]


def test_daystatus_warns_by_mode_and_reads_old_runs():
    from desk import daystatus

    def rate(**kw):
        x = daystatus._facts_empty()
        x.update(default_run_id=1, current_run_id=1, positions_in_force=True, eod_final=True, **kw)
        x["inputs"] = {}
        return daystatus._rate(x)["warnings"]

    def info(**kw):
        x = daystatus._facts_empty()
        x.update(default_run_id=1, current_run_id=1, positions_in_force=True, eod_final=True, inputs={}, **kw)
        return daystatus._rate(x)["info"]

    assert rate() == []
    ex = {"mode": "exclude", "trades": 3, "excluded": 3, "included": 0, "valued": 0, "suspect": 1, "invalid": 0}
    assert rate(off_report=ex) == ["деривативы вне отчёта позиций исключены настройкой: сделок 3",
                                   "возможно, уже в отчёте позиций, не включены: сделок 1"]
    inc = {"mode": "include", "trades": 3, "included": 2, "valued": 2, "suspect": 0, "invalid": 0}
    assert rate(off_report=inc) == []  # chosen by the owner and valued: a line of the status, not a warning
    assert info(off_report=inc) == ["деривативы вне отчёта позиций включены настройкой: сделок 3, включено 2, оценено 2"]
    assert rate(unlinked_swaps=2) == ["условия свопов и РЕПО без строки позиций, не оцениваются: сделок 2"]  # an old run


def test_the_run_carries_the_block_and_the_log_says_it(ctx0, store_url):
    """``lineage.positions.off_report`` and the run log by mode; the hash is the run's own."""
    from desk import store
    from desk.api import coverage

    terms = [fxt("L1"), fxt("N1", vd="2027-01-20"), fxt("D7")]
    rows = [LINKED_ROW, ("OTH|P9", "OTH", {"asset_class": "fx", "ccy": "CNY", "amount_ccy": 1.0, "deal_id": "D7"})]
    out = {}
    with store.connect(store_url) as con:
        for include in (False, True):
            inp = mk(rows, terms)
            c = on(ctx0, include)
            bp = pv1.plan(c, inp)
            summ = offreport.summary(bp.off_report, bp.off_report_matured, "include" if include else "exclude",
                                     sum(1 for pid in bp.added if bp.direct[pid].valuation is not None))
            rid, _, warnings = desk_run.publish(con, c, pv1.merge(bp, []), prev=None, inputs=inp,
                                                core_results=[bp.core_result], off_report=summ)
            lineage = con.execute("SELECT lineage FROM run WHERE run_id = %s", (rid,)).fetchone()[0]
            out[include] = (summ, warnings, coverage.off_report(con, {"lineage": lineage}), lineage["positions"])
    _, ex_warn, ex_cov, ex_pos = out[False]
    assert ex_pos["off_report"]["mode"] == "exclude" and ex_pos["off_report"]["excluded"] == 1
    assert ex_pos["off_report"]["suspect"] == 1 and ex_pos["off_report"]["list"][0]["trade_id"] == "D7"
    assert any(w.startswith("деривативы вне отчёта позиций исключены настройкой: сделок 1") for w in ex_warn)
    assert any(w.startswith("возможно, уже в отчёте позиций: сделок 1") for w in ex_warn)
    assert ex_cov["mode"] == "exclude" and {t["state"] for t in ex_cov["list"]} == {"excluded", "suspect"}
    _, in_warn, in_cov, in_pos = out[True]
    assert in_pos["off_report"]["mode"] == "include" and in_pos["off_report"]["included"] == 1
    assert in_pos["off_report"]["valued"] == 1
    assert any(w.startswith("возможно, уже в отчёте позиций") for w in in_warn)
    assert [t["trade_id"] for t in in_cov["list"]] == ["D7"]  # the included one is a position, not a gap


def test_choosing_the_default_while_the_old_flag_is_on_keeps_an_explicit_exclude(home, monkeypatch):
    update_settings_json({"fx_unlinked_terms": True}, home)
    update_settings_json({"off_report_derivatives": None}, home)  # the page's «исключать (по умолчанию)»
    assert load().off_report_derivatives == "exclude"
    assert "off_report_derivatives" not in settings_routes.settings_payload()  # the list's default, as in force
    # the old variable of the environment (nothing in the file): the list shows what is in force
    update_settings_json({"off_report_derivatives": None}, home)
    monkeypatch.setenv("DESK_FX_UNLINKED_TERMS", "1")
    assert load().off_report_derivatives == "include"
    assert settings_routes.settings_payload()["off_report_derivatives"] == "include"
    update_settings_json({"off_report_derivatives": None}, home)  # the owner chooses «исключать»: it beats the variable
    assert load().off_report_derivatives == "exclude"


def test_the_old_variable_of_the_env_file_counts_too(home):
    (home / "desk.env").write_text("DESK_FX_UNLINKED_TERMS=1\n", encoding="utf-8")
    import os

    os.environ["DESK_ENV_FILE"] = str(home / "desk.env")  # restored by the fixture's monkeypatch of the same name
    assert load().off_report_derivatives == "include"
    update_settings_json({"off_report_derivatives": None}, home)  # «исключать» beats the file's variable
    assert load().off_report_derivatives == "exclude"
    assert "off_report_derivatives" not in settings_routes.settings_payload()
