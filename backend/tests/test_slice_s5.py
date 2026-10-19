"""Slice S5: an EOD run of the golden fixture date published into the desk store, through the CLI too."""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from pathlib import Path

import psycopg
import pytest
from rub_fixed_income import portfolio_result_from_json
from test_slice_s2 import prev_outcome, shifted_prev

from desk import cli, store
from desk.marketdata.adapter import MarketView
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, attach_pnl, prepare, value
from desk.publish import desk_run, tdm_run
from desk.valuation.fallback import CarriedSpread

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
DATE = "2026-10-15"
MODEL = "RU000A0JXQ44"  # its valuation is relabelled as a model price to exercise model_spread


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


@pytest.fixture(scope="module")
def book(ctx):
    rows = [(f"P{i:02d}", "OFZ" if s.startswith("SU") else "CORP", s, 1000.0 * (-1 if i == 3 else 1))
            for i, s in enumerate(sorted(ctx.static.bonds))]
    return rows + [("X01", "CORP", "RU000NOSUCH0", 10.0)]  # not in the static snapshot: not valued


@pytest.fixture(scope="module")
def run_inputs(ctx, book):
    """Outcomes with flash PnL against the synthetic previous date of S2 (exact bonds only)."""
    outs = value(ctx, [(s, q) for _, _, s, q in book], position_ids=[p for p, *_ in book], second_order=True)
    o = next(o for o in outs if o.secid == MODEL)
    o.valuation = dataclasses.replace(o.valuation, quality="model_price", model_spread_source="issuer median Z")
    prev = shifted_prev(ctx, ctx.business_date - timedelta(days=1))
    prevs = [prev_outcome(prev, o) if o.valuation and o.valuation.quality == "exact"
             else Outcome(o.secid, o.terms, None, reason="no_quote") for o in outs]
    attach_pnl(prev, prevs, ctx, outs)
    prev_view = MarketView(ctx.market.snapshot)
    prev_view.business_date = prev.kbd.trade_date
    return outs, prev_view


@pytest.fixture
def con(store_url):
    with store.connect(store_url) as c:
        yield c


def _publish(con, ctx, book, outs, prev, **kw):
    store.load_positions(con, book, valid_date=ctx.business_date, source_name="b.csv", source_sha256="0" * 64)
    inputs = store.positions_for(con, ctx.business_date)
    by_pid = dict(zip([p for p, *_ in book], outs))
    positions = [(pid, bk, by_pid[pid]) for pid, bk, _, _ in inputs.rows]
    return inputs, positions, desk_run.publish(con, ctx, positions, prev=prev, inputs=inputs, **kw)


def test_publish_into_the_store(con, ctx, book, run_inputs):
    outs, prev = run_inputs
    inputs, positions, (rid, status, warnings) = _publish(con, ctx, book, outs, prev)
    assert status == "complete"
    q = lambda sql: con.execute(sql, (rid,)).fetchall()
    assert q("SELECT count(*) FROM run_position WHERE run_id = %s")[0][0] == len(book)
    assert q("SELECT quality FROM run_position WHERE run_id = %s AND position_id = 'X01'") == [(None,)]
    content = tdm_run.run_content(ctx, positions, prev, {})
    assert [m for (m,) in q("SELECT message FROM run_log WHERE run_id = %s ORDER BY seq")][1:] \
        == [m for _, m in content.log][1:]  # the same lines as tdm, after the lineage line
    assert warnings == [m for level, m in content.log if level == "WARN"]
    assert {k for (k,) in q("SELECT kind FROM run_reference WHERE run_id = %s")} == set(tdm_run.REFERENCE_KINDS)

    fact = q("SELECT fact FROM run_position WHERE run_id = %s AND position_id = 'P00'")[0][0]
    row = next(r for r in content.rows if r["position_id"] == "P00")
    assert fact == desk_run._finite(row)  # position_row exactly, NaN as null

    (cov, tot, lin, ids, h, sup), = q("SELECT coverage, totals, lineage, position_set_ids, private_inputs_hash,"
                                      " supersedes_run_id FROM run WHERE run_id = %s")
    assert sum(cov["_total"].values()) == len(book) and cov["_total"]["not_valued"] == 1
    assert cov["CORP"]["model_price"] == 1 and sup is None
    for b in ("OFZ", "CORP", "_total"):
        where = "" if b == "_total" else f" AND book_id = '{b}'"
        pv, dv01, cs01, pnl = q("SELECT sum(pv_rub), sum(dv01_rub), sum(cs01_rub), sum((pnl->'extra'->>'total')::float8)"
                                f" FROM run_position WHERE run_id = %s{where}")[0]
        assert (tot[b]["pv_rub"], tot[b]["dv01_rub"], tot[b]["cs01_rub"]) == pytest.approx((pv, dv01, cs01))
        assert tot[b]["pnl"]["total"] == (None if pnl is None else pytest.approx(pnl))  # no PnL: null, not 0
    assert tot["_total"]["pnl"]["rates"] == pytest.approx(sum(o.pnl.rates for o in outs if o.pnl))
    assert ids == inputs.set_ids and h == inputs.private_inputs_hash == lin["positions"]["private_inputs_hash"]
    for k in ("market_snapshot_id", "static_snapshot_id", "cut_type", "backend_version", "core_version",
              "prev_market_snapshot_id", "kbd_check_bp", "calendar"):
        assert k in lin
    assert lin["positions"]["carried_from"] == {} and lin["model_spreads"] == {"carried": 0, "new": 1}

    shas = {o.core_result.result_sha256 for o in outs if o.core_result}
    stored = q("SELECT result_sha256, body FROM run_core_result WHERE run_id = %s")
    assert {s for s, _ in stored} == shas == {c["result_sha256"] for c in lin["core_results"]}
    for sha, body in stored:
        assert portfolio_result_from_json(body.encode()).result_sha256 == sha  # raises unless the hash verifies

    (z, src), = q("SELECT z_spread_bp, source FROM model_spread WHERE run_id = %s")
    assert src == "issuer median Z" and z == next(o.valuation.z_spread_bp for o in outs if o.secid == MODEL)
    with pytest.raises(psycopg.errors.RaiseException):
        con.execute("UPDATE run SET totals = '{}' WHERE run_id = %s", (rid,))

    # a re-run of the date supersedes the first; the next date carries the model spread and the market Zs
    _, _, (rid2, _, _) = _publish(con, ctx, book, outs, prev)
    assert con.execute("SELECT supersedes_run_id FROM run WHERE run_id = %s", (rid2,)).fetchone()[0] == rid
    carried = store.carried_spreads(con, before=ctx.business_date + timedelta(days=1))
    assert carried[MODEL].z_spread_bp == z and carried[MODEL].source == "issuer median Z"
    exact = {o.secid for o in outs if o.valuation and o.valuation.quality in ("exact", "assumed")}
    assert exact <= set(carried) and f"(run {rid2})" in carried[next(iter(exact))].source


def test_failed_publish_leaves_a_failed_run_and_no_rows(con, ctx, book, run_inputs):
    outs, prev = run_inputs
    store.load_positions(con, book, valid_date=ctx.business_date, source_name="b.csv", source_sha256="0" * 64)
    inputs = store.positions_for(con, ctx.business_date)
    positions = [("P00", "OFZ", outs[0]), ("P00", "OFZ", outs[0])]  # duplicate position: the insert fails
    with pytest.raises(psycopg.errors.UniqueViolation):
        desk_run.publish(con, ctx, positions, prev=prev, inputs=inputs)
    (_rid, status, error), = con.execute("SELECT run_id, status, error FROM run").fetchall()
    assert status == "failed" and "UniqueViolation" in error
    for t in ("run_position", "run_log", "run_reference", "run_core_result", "model_spread"):
        assert con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0


def _cli_env(monkeypatch, tmp_path, store_url, book, name="book.csv"):
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("DESK_STORE_URL", store_url)
    csv = tmp_path / name
    csv.write_text("position_id,book_id,secid,quantity_bonds\n" + "".join(f"{p},{b},{s},{q}\n" for p, b, s, q in book),
                   encoding="utf-8")
    return csv


def test_cli_eod_loads_values_and_publishes(store_url, tmp_path, monkeypatch, capsys, book):
    csv = _cli_env(monkeypatch, tmp_path, store_url, book)
    args = ["eod", "--date", DATE, "--snapshots", str(FIX), "--book", str(csv), "--db", str(tmp_path / "tdm.sqlite")]
    assert cli.main(args + ["--tdm"]) == 0
    assert cli.main(args + ["--books", "OFZ", "--run-type", "EOD"]) == 2  # a partial run is never the EOD run
    first = capsys.readouterr()
    assert "TEST run" in first.err
    assert cli.main(args + ["--books", "OFZ"]) == 0  # same file: no new position set; a TEST run
    assert cli.main(["db-migrate"]) == 0 and cli.main(["runs", "--date", DATE]) == 0
    out = first.out + capsys.readouterr().out
    assert "OFZ rev 1 unchanged" in out and "desk run 1: complete" in out and "tdm run 1: complete" in out
    assert "up to date" in out and "TEST" in out and (tmp_path / "tdm.sqlite").exists()
    assert "PnL" not in out.split("up to date")[1]  # no previous date: no PnL, not "PnL 0"
    with store.connect(store_url) as con:
        assert con.execute("SELECT count(*) FROM position_set").fetchone()[0] == 2
        assert con.execute("SELECT run_type, count(*) FROM run_position JOIN run USING (run_id) WHERE run_id = 2"
                           " GROUP BY 1").fetchone() == ("TEST", sum(b == "OFZ" for _, b, _, _ in book))
        tot = con.execute("SELECT totals FROM run WHERE run_id = 1").fetchone()[0]
        assert all(v is None for b in tot.values() for v in b["pnl"].values()) and tot["_total"]["pv_rub"]


def test_cli_load_positions_closes_and_rejects(store_url, tmp_path, monkeypatch, capsys, book):
    csv = _cli_env(monkeypatch, tmp_path, store_url, book)
    assert cli.main(["load-positions", str(csv), "--date", DATE]) == 0
    ofz = _cli_env(monkeypatch, tmp_path, store_url, [r for r in book if r[1] == "OFZ"], "ofz.csv")
    assert cli.main(["load-positions", str(ofz), "--date", DATE, "--books", "OFZ,CORP"]) == 0
    assert "CORP rev 2, OFZ rev 1 unchanged" in capsys.readouterr().out
    dup = _cli_env(monkeypatch, tmp_path, store_url, book + book[:1], "dup.csv")
    assert cli.main(["load-positions", str(dup), "--date", DATE]) == 1
    assert cli.main(["eod", "--date", DATE, "--snapshots", str(FIX), "--book", str(dup)]) == 1
    err = capsys.readouterr().err
    assert err.count(f"desk: dup.csv: position ids twice in the file: {book[0][0]}") == 2
    with store.connect(store_url) as con:
        assert not [r for r in store.positions_for(con, date.fromisoformat(DATE)).rows if r[1] == "CORP"]


def test_cli_revalues_the_previous_date_with_the_same_carried_spreads(store_url, tmp_path, monkeypatch, book):
    """The previous date's revaluation for the flash PnL gets the carried spreads of the run's date (S4)."""
    csv = _cli_env(monkeypatch, tmp_path, store_url, book)
    sentinel = {MODEL: CarriedSpread(MODEL, 123.0, date.fromisoformat(DATE) - timedelta(days=7), "test")}
    monkeypatch.setattr(store, "carried_spreads", lambda con, before: sentinel)
    monkeypatch.setattr(SnapshotDir, "previous_market_date", lambda self, d: DATE)  # the fixture has one date,
    monkeypatch.setattr(cli, "attach_pnl", lambda *a: None)  # so no PnL between the "two" dates
    calls = []

    def recording(ctx, requests, **kw):
        calls.append(kw)
        return value(ctx, requests, **kw)

    monkeypatch.setattr(cli, "value", recording)
    assert cli.main(["eod", "--date", DATE, "--snapshots", str(FIX), "--book", str(csv), "--report-dir", ""]) == 0
    assert len(calls) == 2 and all(c["carried_spreads"] is sentinel for c in calls)
