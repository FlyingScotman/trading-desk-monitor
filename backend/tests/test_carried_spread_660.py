"""TDM#660, the plan of the history notices a stale carried spread: a date whose model price carried a spread from an
earlier date is made again when that date holds another spread now (its run was made again with another value), as far as
the carry reaches (a chain of dates), never because a run was only replaced (the second pass of #627 and the pruning of
#634 leave the same spread under another run id), and a run from before the sources were recorded is told unknown, not
stale. The decision does not depend on the processes of the pool. Synthetic data only: ``NOQ`` is priced on the first date
and valued by model, from the carried spread, on the later ones (``cache_noq``)."""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from test_market_history import PRICES, shifted, write
from test_parallel_backfill_628 import ALL_NOQ, NOQ, cache_noq, load_noq, second_store  # noqa: F401 - the fixture

from desk import backfill, store
from desk.settings import Settings

D_FROM, D_TO = date(2026, 8, 25), date(2026, 10, 13)
DATES = ["2026-09-01", "2026-10-09", "2026-10-12", "2026-10-13"]  # priced, then a chain of three model-priced dates
NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def setup(url: str, tmp_path: Path, cache: Path, workers: int = 1) -> Settings:
    load_noq(url, tmp_path, "2026-09-01", 10)
    return Settings(home=tmp_path / f"home_{uuid.uuid4().hex[:6]}", snapshot_cache=cache, store_url=url,
                    backfill_workers=workers)


def plan(url: str, s: Settings) -> backfill.Plan:
    with store.connect(url) as con:
        return backfill.plan(con, s, D_FROM, D_TO)


def both_passes(url: str, s: Settings, workers: int = 1) -> None:
    r1 = backfill.run(s, plan(url, s), store_url=url, out=lambda line: None, workers=workers, now=NOW)
    assert r1["done"] and not r1["failed"] and r1["complete"], r1
    r2 = backfill.run_second(s, plan(url, s), store_url=url, out=lambda line: None, workers=workers)
    assert not r2["failed"], r2


def default(url: str) -> dict[str, dict[str, Any]]:
    with store.connect(url) as con:
        return {d.isoformat(): r for d, r in store.default_runs(con, date(2026, 8, 1)).items()}


def sources(url: str) -> dict[str, Any]:
    return {d: r["spread_sources"] for d, r in default(url).items()}


def why(p: backfill.Plan) -> dict[str, str | None]:
    return {d.date: d.why for d in p.todo}


@contextlib.contextmanager
def unfrozen(url: str) -> Iterator[Any]:
    """A run is immutable in service; the triggers are off for the statements of a test that stands for what another
    version, a failed run or a recalculation leaves in the store."""
    with store.connect(url) as con:
        for t, trg in (("run", "run_frozen"), ("model_spread", "model_spread_frozen")):
            con.execute(f"ALTER TABLE {t} DISABLE TRIGGER {trg}")
        try:
            yield con
        finally:
            for t, trg in (("run", "run_frozen"), ("model_spread", "model_spread_frozen")):
                con.execute(f"ALTER TABLE {t} ENABLE TRIGGER {trg}")
            backfill._CARRY_MEMO.clear()  # (the plan remembers the spreads of a run: in service a run never changes)


def shift_spread(url: str, d: str, by: float) -> float:
    """The date's default run made again with another spread, as a recalculation after a change of its inputs leaves it:
    its model spread of ``NOQ`` moved (the row of the run, what the next date carries from)."""
    rid = default(url)[d]["run_id"]
    with unfrozen(url) as con:
        row = con.execute("SELECT z_spread_bp FROM model_spread WHERE run_id = %s AND secid = %s", (rid, NOQ)).fetchone()
        con.execute("UPDATE model_spread SET z_spread_bp = z_spread_bp + %s WHERE run_id = %s AND secid = %s",
                    (by, rid, NOQ))
    return row[0] + by


def correct_first_date(cache: Path, by: str = "1.5") -> None:
    """The hub corrects the first date's close: the price of ``NOQ`` on it is another (a later ``known_at``), so its market
    spread, which the later dates carry, is another."""
    from decimal import Decimal

    doc = shifted(ALL_NOQ[0], len(ALL_NOQ), light=False)
    doc["known_at"] = "2026-09-01T20:00:00Z"
    for q in doc["data"]["bond_quotes"]:
        if q["secid"] == NOQ:
            for k in PRICES:
                if q.get(k) is not None:
                    q[k] = str(Decimal(q[k]) - Decimal(by))
    write(cache, doc)


def test_a_run_records_the_date_and_the_value_of_the_spread_its_model_price_carried(tmp_path, cache_noq, store_url):  # noqa: F811
    s = setup(store_url, tmp_path, cache_noq)
    both_passes(store_url, s)
    src = sources(store_url)
    assert src["2026-09-01"] == {}, "the first date has its own price: nothing carried"
    assert src["2026-10-09"][NOQ]["date"] == "2026-09-01"  # the market Z of the last priced date
    assert src["2026-10-12"][NOQ]["date"] == "2026-10-09" and src["2026-10-13"][NOQ]["date"] == "2026-10-12"
    z = {d: v[NOQ]["z_bp"] for d, v in src.items() if v}
    assert len(set(z.values())) == 1, "the same spread is carried along the chain (the model price moves with the curve)"
    p = plan(store_url, s)
    assert [d.status for d in p.days] == ["done"] * 4 and p.summary()["carry_unknown"] == 0 and not p.todo
    assert p.summary()["second"]["done"] == 4


def test_a_run_replaced_by_the_second_pass_or_pruned_is_no_change_of_the_spread_it_carried(tmp_path, cache_noq, store_url):  # noqa: F811
    """Every first-pass run is replaced by the second pass's (#627), and the replaced runs are deleted after a while (#634):
    the dates after it still carry the same spread, from the same date, and are not made again for that."""
    s = setup(store_url, tmp_path, cache_noq)
    r1 = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, now=NOW)
    first = {d: r["run_id"] for d, r in default(store_url).items()}
    assert r1["complete"] and not plan(store_url, s).todo
    backfill.run_second(s, plan(store_url, s), store_url=store_url, out=lambda line: None)
    assert all(default(store_url)[d]["run_id"] > first[d] for d in DATES), "the second pass replaced every run"
    p = plan(store_url, s)
    assert not p.todo and [d.status for d in p.days] == ["done"] * 4, why(p)
    with unfrozen(store_url) as con:
        con.execute("UPDATE run SET completed_at = completed_at - interval '30 days'")  # the replaced runs are old enough
    with store.connect(store_url) as con:
        pruned = store.prune_superseded(con, 7)["runs"]
    assert set(pruned) == set(first.values()), "the scenario: the runs the dates carried from are gone"
    p = plan(store_url, s)
    assert not p.todo and [d.status for d in p.days] == ["done"] * 4 and p.summary()["carry_unknown"] == 0, why(p)


def test_a_date_made_again_with_another_spread_makes_the_dates_that_carried_from_it_stale_down_the_chain(
        tmp_path, cache_noq, store_url):  # noqa: F811
    s = setup(store_url, tmp_path, cache_noq)
    both_passes(store_url, s)
    old = default(store_url)
    moved = shift_spread(store_url, "2026-10-09", 2.5)
    p = plan(store_url, s)
    assert why(p) == {"2026-10-12": backfill.CARRY_WHY, "2026-10-13": backfill.CARRY_WHY}, why(p)
    assert [d.status for d in p.days] == ["done", "done", "todo", "todo"]  # the changed date itself, and its source, are not
    assert p.summary()["todo_by_reason"] == [{"key": "carry", "text": "перенесённый спред устарел", "n": 2}]
    with store.connect(store_url) as con:  # what the log of a date that does not converge says
        day = next(d for d in p.days if d.date == "2026-10-12")
        assert f"carried spread of {NOQ}" in backfill._differs(con, day) and f"{moved:g} bp there now" in backfill._differs(con, day)
    r = backfill.run(s, p, store_url=store_url, out=lambda line: None, now=NOW)
    assert r["done"] == ["2026-10-12", "2026-10-13"] and not r["failed"] and not r["unconverged"], r
    new = default(store_url)
    assert new["2026-10-12"]["run_id"] > old["2026-10-12"]["run_id"] and new["2026-10-09"]["run_id"] == old["2026-10-09"]["run_id"]
    for d, src in (("2026-10-12", "2026-10-09"), ("2026-10-13", "2026-10-12")):  # the new value is carried on
        got = new[d]["spread_sources"][NOQ]
        assert got["date"] == src and got["z_bp"] == pytest.approx(moved, abs=1e-6), got
    assert not plan(store_url, s).todo, "the plan converges: nothing is stale after the dates were made again"


def test_a_hub_correction_of_the_first_date_makes_the_whole_chain_stale_and_the_plan_converges(
        tmp_path, cache_noq, store_url):  # noqa: F811
    """The whole story without a hand on the store: the first date's close is corrected, it is made again, and the three
    dates that carried its spread, one from the other, are made again after it, in this run, with the new spread."""
    s = setup(store_url, tmp_path, cache_noq)
    both_passes(store_url, s)
    old = default(store_url)
    correct_first_date(cache_noq)
    p = plan(store_url, s)
    # (10.09 is made again for its own reason too: its previous close is the corrected one)
    assert why(p) == {"2026-09-01": "market snapshot changed", "2026-10-09": "previous snapshot changed",
                      "2026-10-12": backfill.CARRY_WHY, "2026-10-13": backfill.CARRY_WHY}, why(p)
    r = backfill.run(s, p, store_url=store_url, out=lambda line: None, now=NOW)
    assert r["done"] == DATES and not r["failed"] and not r["unconverged"], r
    new = default(store_url)
    z0, z1 = old["2026-10-09"]["spread_sources"][NOQ]["z_bp"], new["2026-10-09"]["spread_sources"][NOQ]["z_bp"]
    assert z0 != z1, "the scenario: the corrected price moved the spread"
    assert all(new[d]["spread_sources"][NOQ]["z_bp"] == pytest.approx(z1, abs=1e-6) for d in DATES[1:]), \
        "the new spread went down the chain"
    assert not why(plan(store_url, s)), why(plan(store_url, s))
    backfill.run_second(s, plan(store_url, s), store_url=store_url, out=lambda line: None)  # the second pass changes nothing
    assert not why(plan(store_url, s)) and plan(store_url, s).summary()["second"]["todo"] == 0


def test_a_date_to_be_made_again_makes_the_dates_that_carried_from_it_stale_before_it_is_done(tmp_path, cache_noq, store_url):  # noqa: F811
    """The chain is in the plan at once, not one date a run: 10.09 is made again for its own reason, so 10.12 and 10.13
    (carried from it, one from the other) are listed with it, though their runs still hold the old spread."""
    s = setup(store_url, tmp_path, cache_noq)
    both_passes(store_url, s)
    rid = default(store_url)["2026-10-09"]["run_id"]
    with unfrozen(store_url) as con:
        con.execute("UPDATE run SET market_snapshot_id = 'older-snapshot' WHERE run_id = %s", (rid,))  # a hub correction
    p = plan(store_url, s)
    assert why(p) == {"2026-10-09": "market snapshot changed", "2026-10-12": backfill.CARRY_WHY,
                      "2026-10-13": backfill.CARRY_WHY}, why(p)
    assert {x["key"]: x["n"] for x in p.summary()["todo_by_reason"]} == {"carry": 2, "market": 1}


def test_a_source_date_without_a_run_makes_its_dates_stale(tmp_path, cache_noq, store_url):  # noqa: F811
    s = setup(store_url, tmp_path, cache_noq)
    both_passes(store_url, s)
    with unfrozen(store_url) as con:  # no run of 10.09 stands (its runs failed, were removed): there is nothing to carry from
        con.execute("UPDATE run SET status = 'failed' WHERE business_date = '2026-10-09'")
    assert why(plan(store_url, s)) == {"2026-10-09": "no default run", "2026-10-12": backfill.CARRY_WHY,
                                       "2026-10-13": backfill.CARRY_WHY}


def test_a_run_from_before_the_sources_were_recorded_is_unknown_not_stale(tmp_path, cache_noq, store_url):  # noqa: F811
    s = setup(store_url, tmp_path, cache_noq)
    both_passes(store_url, s)
    with unfrozen(store_url) as con:  # runs made by an older version: no ``carried_spread_sources`` in the lineage
        con.execute("UPDATE run SET lineage = lineage - 'carried_spread_sources' WHERE business_date >= '2026-10-12'")
    shift_spread(store_url, "2026-10-09", 2.5)  # what would make a recorded run stale
    p = plan(store_url, s)
    assert not p.todo, why(p)
    assert [d.carry for d in p.days] == [None, None, "unknown", "unknown"]
    assert p.summary()["carry_unknown"] == 2
    with unfrozen(store_url) as con:  # a legacy run that says it carried nothing is known: nothing to be stale
        con.execute("UPDATE run SET lineage = jsonb_set(lineage, '{model_spreads,carried}', '0')"
                    " WHERE business_date = '2026-10-13'")
    p = plan(store_url, s)
    assert [d.carry for d in p.days] == [None, None, "unknown", None] and p.summary()["carry_unknown"] == 1


def test_the_plan_says_the_same_with_the_pool_on_and_off(tmp_path, cache_noq, pg_server):  # noqa: F811
    """The runs of the pool carry the same spreads (``dump`` of #628 compares the lineage), so the plan decides alike;
    here the whole story: the history in both passes, a date made again with another spread, the plan, its run."""
    out = []
    for workers in (1, 3):
        url = second_store(pg_server)
        s = setup(url, tmp_path, cache_noq, workers)
        both_passes(url, s, workers)
        src1 = sources(url)
        moved = shift_spread(url, "2026-10-09", 2.5)
        p = plan(url, s)
        before = (why(p), p.summary()["todo_by_reason"])
        r = backfill.run(s, p, store_url=url, out=lambda line: None, workers=workers, now=NOW)
        assert r["done"] == ["2026-10-12", "2026-10-13"] and not r["failed"], r
        after = (sources(url), plan(url, s).summary())
        out.append((src1, moved, before, json.dumps(after, sort_keys=True, default=str)))
    assert out[0] == out[1]
    assert out[0][2][0] == {"2026-10-12": backfill.CARRY_WHY, "2026-10-13": backfill.CARRY_WHY}
