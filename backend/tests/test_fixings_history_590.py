"""TDM#590: a run fills the fixings of its market cut before the cut's window from the monitor's stored history
(``marketdata.fixing_history``, ``md_hub_fixing``): an OIS on RUONIA whose one-year period started before the window
is valued as on a cut with the whole history, and says where its fixings came from. On the hub's contract 1.4 golden
cut of 15.10.2026 (its fixings start on 01.10: the window) with an invented history of September and invented
trades."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta

import pytest
from test_swaps_repo_tdm45 import BD, FIX, leg

from desk import backfill, cli, store
from desk.marketdata import fixing_history as fh
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.settings import Settings
from desk.store import PositionInputs
from desk.valuation import positions as pv1

SEPT = [date(2026, 9, 1) + timedelta(days=n) for n in range(30)]
WEEKDAYS = [d for d in SEPT if d.weekday() < 5]


def row(code, d, value="16.25", source="cbr"):
    return {"index_code": code, "source": source, "fixing_date": d.isoformat(), "value": value, "unit": "pct",
            "effective_date": None, "published_on": None, "published_at": None, "hub_ingested_at": None, "labels": [],
            "origin": fh.SOURCE_ID}


def stored(extra=()):
    rows = [row("RUONIA", d) for d in WEEKDAYS] + [row("KEY_RATE", d, "16.5") for d in SEPT] + list(extra)
    by: dict[str, list] = {}
    for r in sorted(rows, key=lambda r: (r["index_code"], r["fixing_date"])):
        by.setdefault(r["index_code"], []).append(r)
    return fh.FixingStore({k: tuple(v) for k, v in by.items()}, ("synthetic",))


def swap(tid="Y1"):
    """A payer OIS on RUONIA with one annual period that started on 14.09, before the cut's window."""
    kw = {"start_date": "2026-09-14", "maturity_date": "2027-09-14", "trade_date": "2026-09-10",
          "payment_frequency": "1Y", "day_count": "ACT/365"}
    return [leg(tid, "fix", "pay", fixed_rate_pct="15.5", **kw), leg(tid, "flt", "receive", index_code="RUONIA", **kw)]


def value(ctx, terms):
    x = {"Y1": {"asset_class": "otc", "ccy": "RUB", "source_instrument_id": "Y1", "deal_id": "Y1"}}
    inp = PositionInputs([("Y1", "IRB", None, None)], [], {"IRB": BD}, datetime.now(UTC), "0" * 64, x,
                         trade_terms=terms)
    return pv1.plan(ctx, inp).direct["Y1"]


@pytest.fixture(scope="module")
def plain():
    return prepare(SnapshotDir(FIX), BD.isoformat())


@pytest.fixture(scope="module")
def filled():
    return prepare(SnapshotDir(FIX), BD.isoformat(), fixings=stored())


def test_the_cut_alone_refuses_the_swap(plain):
    o = value(plain, swap())
    assert o.valuation is None or o.core_not_valued is not None
    reason = o.core_not_valued[0] if o.core_not_valued else o.reason
    assert reason == "core_missing_market_data" and "RUONIA" in (o.core_not_valued or ("", o.detail))[1]


def test_the_stored_history_fills_the_window_and_the_swap_is_valued(plain, filled):
    o = value(filled, swap())
    v = o.valuation
    assert v is not None and o.core_not_valued is None and v.extra["core"]["product"] == "ois"
    sw = v.extra["swaps"][0]
    assert sw["fixing_id"] == "RUONIA" and sw["fixing_snapshot_id"] == f"{filled.market.snapshot.id}+md_hub_fixing"
    lin = filled.rates["fixings_history"]["codes"]
    assert lin["RUONIA"] == {"rows": len(WEEKDAYS), "from": "2026-09-01", "to": "2026-09-30",
                             "window_from": "2026-10-01", "sources": ["cbr"]}
    assert filled.rates["fixings"]["RUONIA"]["from_history"]["rows"] == len(WEEKDAYS)
    assert filled.rates["fixings"]["RUONIA"]["first"] == "2026-09-01"
    # the cut's own rows are as they were, and the market snapshot is the run's
    own = [f for f in plain.market.fixings if f["index_code"] == "RUONIA"]
    assert [f for f in filled.market.fixings if f["index_code"] == "RUONIA" and f["fixing_date"] >= "2026-10-01"] == own
    assert filled.market.snapshot.id == plain.market.snapshot.id


def test_only_dates_before_the_window_and_the_cuts_own_sources():
    snap = SnapshotDir(FIX).market(BD.isoformat(), "eod_final")
    extra = [row("RUONIA", date(2026, 10, 1), "99"),  # a date the cut has: never replaced
             row("RUONIA", date(2026, 9, 2), "17", source="other_source"),  # a source the cut does not read
             row("RUONIA", date(2014, 1, 9)),  # older than ten years before the date
             row("NOT_IN_CUT", date(2026, 9, 1))]  # an index the cut does not carry
    out, lin = fh.extend(snap, stored(extra))
    ru = [f for f in out.data["fixings"] if f["index_code"] == "RUONIA"]
    assert next(f for f in ru if f["fixing_date"] == "2026-10-01")["value"] != "99"
    assert {f["source"] for f in ru} == {"cbr"} and min(f["fixing_date"] for f in ru) == "2026-09-01"
    assert "NOT_IN_CUT" not in lin["codes"] and lin["depth_from"] == "2016-10-15"
    assert snap.data["fixings"] is not out.data["fixings"]  # the snapshot itself is not changed
    assert fh.extend(snap, fh.FixingStore())[0] is snap


def insert(con, rows):
    con.execute("INSERT INTO md_hub_job (dataset, key, params, period_from, period_to, status, fetched_at, checked_at)"
                " VALUES ('fixings', '2026', '{}', '2026-01-01', '2026-12-31', 'done', now(), now())")
    for r in rows:
        con.execute("INSERT INTO md_hub_fixing (index_code, source, fixing_date, value, unit, labels, fetch_id)"
                    " VALUES (%s, %s, %s, %s, 'pct', '[]', 1)", (r["index_code"], r["source"], r["fixing_date"],
                                                                 r["value"]))


def test_load_status_and_the_card(tmp_path, store_url):
    with store.connect(store_url) as con:
        insert(con, [row("RUONIA", d) for d in WEEKDAYS[:3]] + [row("SOFR", WEEKDAYS[0], source="nyfed")])
        got = fh.load(con, date(2016, 1, 1))
        assert [r["fixing_date"] for r in got.rows["RUONIA"]] == [d.isoformat() for d in WEEKDAYS[:3]]
        assert got.rows["RUONIA"][0]["value"] == "16.25" and got.rows["SOFR"][0]["source"] == "nyfed"
        assert fh.load(con, date(2016, 1, 1)) is got  # unchanged: the rows read before
        st = fh.status(con)
        assert st["indices"] == 2 and st["rows"] == 4 and [c["code"] for c in st["codes"]] == ["RUONIA", "SOFR"]
        assert st["jobs"]["done"] == 1 and st["from"] == "2026-09-01"
        s = Settings(home=tmp_path / "home", snapshot_cache=tmp_path / "cache", store_url=store_url)
        doc = backfill.status(con, s, today=date(2026, 10, 15))
    assert doc["fixings_history"]["rows"] == 4 and doc["fixings_history"]["loading"] is False
    assert cli.stored_fixings(store_url, BD).rows["RUONIA"][0]["fixing_date"] == "2026-09-01"
    assert cli.stored_fixings("postgresql://nobody@127.0.0.1:1/none", BD, out=lambda s: None) is None


def test_a_history_before_the_hubs_calendar_is_its_own_publication_calendar(plain):
    """The hub's calendars start on 01.01.2026 here: a period that began in December 2025 needs RUONIA before them.
    There the days with a fixing are the publication calendar (a weekday without one a holiday), not a guess."""
    from desk.curves.rates import ruonia_calendar

    cal, _ = ruonia_calendar(plain.static.snapshot, BD)
    assert cal.valid_from == date(2026, 1, 1)
    days = [date(2025, 12, 1) + timedelta(days=n) for n in range(304)]  # 01.12.2025 .. 30.09.2026
    have = [d for d in days if (d.weekday() < 5 and d != date(2025, 12, 31)) if d < cal.valid_from] + \
        [d for d in days if d >= cal.valid_from and cal.is_business_day(d)]
    ctx = prepare(SnapshotDir(FIX), BD.isoformat(), fixings=fh.FixingStore(
        {"RUONIA": tuple(row("RUONIA", d) for d in have)}, ("before-calendar",)))
    kw = {"start_date": "2025-12-15", "maturity_date": "2026-12-15", "trade_date": "2025-12-11",
          "payment_frequency": "1Y", "day_count": "ACT/365"}
    o = value(ctx, [leg("Y1", "fix", "pay", fixed_rate_pct="15.5", **kw),
                    leg("Y1", "flt", "receive", index_code="RUONIA", **kw)])
    assert o.valuation is not None and o.core_not_valued is None, o.core_not_valued
    info = ctx.rates["fixings"]["RUONIA"]
    assert info["calendar_from_fixings"] == {"from": "2025-12-01", "to": "2025-12-31"}
    assert info["dropped_before_calendar"] == 0 and info["first"] == "2025-12-01"
    assert info["missing_business_days"] == []  # 31.12.2025 had no fixing: a holiday of the publication calendar


def test_stored_rows_of_another_unit_or_two_sources_a_date_are_not_taken():
    """Review of TDM#590: a stored row without a unit or in another one, and a second source of a date, would make the
    run's reading of the index fail whole (``adapter_rates.fixings`` refuses them): they never go under the cut."""
    snap = SnapshotDir(FIX).market(BD.isoformat(), "eod_final")
    bad = [{**row("RUONIA", date(2026, 9, 3)), "unit": None}, {**row("RUONIA", date(2026, 9, 4)), "unit": "bp"},
           {**row("KEY_RATE", date(2026, 9, 2), "17"), "source": "cbr_mirror"}]
    out, lin = fh.extend(snap, stored(bad))
    ru = {f["fixing_date"]: f for f in out.data["fixings"] if f["index_code"] == "RUONIA"}
    assert ru["2026-09-03"]["unit"] == "pct" and ru["2026-09-04"]["unit"] == "pct"
    key = [f for f in out.data["fixings"] if f["index_code"] == "KEY_RATE" and f["fixing_date"] == "2026-09-02"]
    assert len(key) == 1 and key[0]["source"] == "cbr"  # the cut's source; a mirror the cut does not read is out
    # two rows of one date from two sources the cut reads: one, by the preference order
    two = fh.FixingStore({"SOFR": (row("SOFR", date(2026, 9, 1), "4.1", "fred"), row("SOFR", date(2026, 9, 1), "4.3",
                                                                                   "nyfed"))}, ("x",))
    sofr = [{**row("SOFR", date(2026, 10, 8), "4.3", "nyfed")}, {**row("SOFR", date(2026, 10, 8), "4.1", "fred")}]
    cut = dataclasses.replace(snap, doc={**snap.doc, "data": {**snap.data, "fixings": [*snap.data["fixings"], *sofr]}})
    out, lin = fh.extend(cut, two)
    got = [f for f in out.data["fixings"] if f["index_code"] == "SOFR" and f["fixing_date"] == "2026-09-01"]
    assert [f["source"] for f in got] == ["nyfed"] and lin["codes"]["SOFR"]["rows"] == 1
    # a value that is no number: that index is not filled, said, the others are
    out, lin = fh.extend(snap, stored([{**row("RUONIA", date(2026, 9, 1)), "value": "n/a"}]))
    assert "RUONIA" not in lin["codes"] and "RUONIA" in lin["errors"] and "KEY_RATE" in lin["codes"]
    ctx = prepare(SnapshotDir(FIX), BD.isoformat(), fixings=stored(bad))
    assert ctx.rates["status"]["fixings"] == "ok" and ctx.rates["status"]["key_rate_swap"] == "ok"


def test_a_history_that_breaks_the_runs_reading_is_dropped_whole(monkeypatch):
    """Whatever slips through: the run's fixings fail with the stored history, so the run goes on without it, said."""
    from desk.curves import rates as R

    real = R.fixing_entries

    def picky(market, static, *a, **kw):
        if fh.filled(market, "RUONIA"):
            raise ValueError("synthetic: the stored rows do not read")
        return real(market, static, *a, **kw)

    monkeypatch.setattr(R, "fixing_entries", picky)
    ctx = prepare(SnapshotDir(FIX), BD.isoformat(), fixings=stored())
    fhl = ctx.rates["fixings_history"]
    assert ctx.rates["status"]["fixings"] == "ok" and fhl["codes"] == {} and "synthetic" in fhl["dropped"]


def test_a_long_hole_in_the_history_is_missing_data_not_holidays(plain):
    """Review of TDM#590: before the hub's calendar a weekday without a fixing is a holiday only in a short run (the
    New Year holidays at most); a hole of weeks (a load that failed) is missing data: core refuses the period over it
    instead of carrying one rate across it."""
    from desk.curves.rates import PUBLISHED_HOLIDAY_MAX_DAYS, published_before, ruonia_calendar

    cal, _ = ruonia_calendar(plain.static.snapshot, BD)
    days = [date(2025, 11, 3) + timedelta(days=n) for n in range(59)]  # 03.11.2025 .. 31.12.2025
    hole = {d for d in days if date(2025, 11, 10) <= d <= date(2025, 11, 28)}  # 15 weekdays
    short = {date(2025, 12, 8), date(2025, 12, 9)}
    have = [d for d in days if d.weekday() < 5 and d not in hole and d not in short]
    out, rng = published_before(cal, have)
    assert PUBLISHED_HOLIDAY_MAX_DAYS == 8 and not (hole & out.holidays) and short <= out.holidays
    # the calendar starts after the hole: the rows before it are dropped, the hole is said
    assert rng == {"from": "2025-11-29", "to": "2025-12-31", "hole": {"from": "2025-11-10", "to": "2025-11-28"}}
    assert out.valid_from == date(2025, 11, 29)
    # a run: a swap whose period began before the hole finds no fixing there (core refuses it), one after it is valued
    ctx = prepare(SnapshotDir(FIX), BD.isoformat(), fixings=fh.FixingStore({"RUONIA": tuple(
        row("RUONIA", d) for d in have + [d for d in (date(2026, 1, 1) + timedelta(days=n) for n in range(273))
                                          if cal.is_business_day(d)])}, ("hole",)))
    info = ctx.rates["fixings"]["RUONIA"]
    assert info["calendar_from_fixings"]["hole"] == {"from": "2025-11-10", "to": "2025-11-28"}
    assert info["first"] == "2025-12-01" and info["missing_business_days"] == []

    def ois(start, end):
        kw = {"start_date": start, "maturity_date": end, "trade_date": start, "payment_frequency": "1Y",
              "day_count": "ACT/365"}
        return value(ctx, [leg("Y1", "fix", "pay", fixed_rate_pct="15.5", **kw),
                           leg("Y1", "flt", "receive", index_code="RUONIA", **kw)])

    before, after = ois("2025-11-05", "2026-11-05"), ois("2025-12-15", "2026-12-15")
    assert before.valuation is None and before.core_not_valued[0] in ("core_missing_market_data",
                                                                       "core_calendar_out_of_range")
    assert after.valuation is not None and after.core_not_valued is None


def test_the_scenario_and_the_carry_reprice_on_the_stored_history_too(monkeypatch, store_url):
    """Review of TDM#590: the exact scenario and the forward carry rebuild a run's context: from the same stored
    history, so a swap or a floater the run valued is repriced on the same fixings."""
    import inspect

    from desk.scenario import carry, exact, jobs

    run = {"run_id": -590, "business_date": BD, "cut_type": "eod_final", "market_snapshot_id": None}
    ctx = exact._context(SnapshotDir(FIX), run, None, stored())
    assert ctx.rates["fixings_history"]["codes"]["RUONIA"]["rows"] == len(WEEKDAYS)
    assert "fixing_history.for_date(con, run[\"business_date\"])" in inspect.getsource(jobs.run_once)
    assert "fixing_history.for_date(con, run[\"business_date\"])" in inspect.getsource(carry._forward_carry)
    seen = {}
    monkeypatch.setattr("desk.orchestrator.prepare", lambda *a, **kw: seen.update(kw) or (_ for _ in ()).throw(
        RuntimeError("stop")))
    from desk.report import instruments

    with pytest.raises(RuntimeError):
        instruments._core_market([], FIX, SnapshotDir(FIX).market(BD.isoformat(), "eod_final"), stored())
    assert seen["fixings"].stamp == ("synthetic",)
    with store.connect(store_url) as con:
        assert fh.for_date(con, BD).rows == {}
