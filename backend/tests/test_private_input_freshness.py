"""EOD freshness on canonical trade terms, using the reference CSV reader and a temporary PostgreSQL store."""
from __future__ import annotations

import csv
from datetime import date

import pytest
from test_r23_backfill import cache, load, settings  # noqa: F401 - shared synthetic market fixture

from desk import backfill, cli, store
from desk.autopilot import Autopilot
from desk.inbox import scan

DAY = date(2026, 10, 15)
FIELDS = ("trade_id", "book_id", "product_kind", "trade_date", "value_date", "base_ccy", "quote_ccy", "side",
          "notional_base", "notional_quote", "contract_rate", "x_note")


@pytest.fixture
def env(tmp_path, cache, store_url):  # noqa: F811 - imported pytest fixture
    load(store_url, tmp_path, DAY.isoformat(), 10)
    cfg, inbox = tmp_path / "config", tmp_path / "inbox"
    cfg.mkdir()
    inbox.mkdir()
    (cfg / "books.csv").write_text("book_id;name;desk;book_type;entity;active\nOFZ;Synthetic;Test;TRADING;TEST;1\n"
                                    "UNUSED;Synthetic;Test;TRADING;TEST;1\n", encoding="utf-8")
    s = settings(tmp_path, cache, store_url, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive",
                 private_adapter_config=cfg, inbox_poll_seconds=0, inbox_lookback_days=0)
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield s
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


def ingest(con, s, seq, *, rate=80, empty=False, note="synthetic", day=DAY, book="OFZ"):
    path = s.inbox_dirs[0] / f"trade_terms_{day:%Y%m%d}_{seq}.csv"
    row = {"book_id": book} if empty else dict(zip(FIELDS, (
        "TRADE-1", book, "fx_forward", "2026-10-01", "2026-12-17", "USD", "RUB", "buy", 10, 10 * rate, rate, note)))
    with path.open("w", newline="", encoding="utf-8-sig") as out:
        writer = csv.DictWriter(out, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerow(row)
    result = scan.scan(con, s)
    assert result.ok and result.files.get("accepted") == 1, result.text()


def planner(con, s):
    return backfill.plan(con, s, DAY, DAY)


def current(con):
    return store.default_runs(con, DAY)[DAY]


def run_scheduler(con, s, path, ap):
    if path == "backfill":
        return backfill.run(s, planner(con, s), store_url=s.store_url, out=lambda _: None)
    return ap._eod(s, con, s.store_url, [], [])


def run_count(con):
    return con.execute("SELECT count(*) FROM run").fetchone()[0]


@pytest.mark.parametrize("path", ["backfill", "eod"])
def test_terms_import_correction_deletion_recompute_and_canonical_noops_do_not(env, path):
    s = env
    ap = Autopilot(settings_loader=lambda: s, today=lambda: DAY)
    with store.connect(s.store_url) as con:
        cli.eod_run(s, DAY.isoformat(), report_dir=None, out=lambda _: None)
        positions = store.current_sets(con, DAY)[0]
        for seq, changes in ((1, {}), (4, {"rate": 81}), (5, {"empty": True})):
            old = current(con)
            ingest(con, s, seq, **changes)
            inputs = store.positions_for(con, DAY)
            assert store.current_sets(con, DAY)[0] == positions
            # The saved hash is read directly as well: this regression is RED before default_runs exposes it.
            old_hash = con.execute("SELECT private_inputs_hash FROM run WHERE run_id = %s", (old["run_id"],)).fetchone()[0]
            assert old_hash != inputs.private_inputs_hash
            if path == "backfill":
                assert planner(con, s).days[0].status == "todo"
                assert planner(con, s).days[0].why == "private inputs changed"
            run_scheduler(con, s, path, ap)
            new = current(con)
            assert new["run_id"] != old["run_id"]
            assert new["private_inputs_hash"] == inputs.private_inputs_hash
            row = con.execute("SELECT status, private_inputs_hash, lineage FROM run WHERE run_id = %s",
                              (new["run_id"],)).fetchone()
            assert row[0] == "complete" and row[1] == inputs.private_inputs_hash
            assert row[2]["positions"]["private_inputs_hash"] == inputs.private_inputs_hash
            assert bool(row[2]["positions"].get("trade_terms"))
            assert planner(con, s).days[0].status == "done"
            before = run_count(con)
            run_scheduler(con, s, path, ap)
            assert run_count(con) == before
            if seq == 1:
                # Re-read identical accepted bytes, then accept a metadata-only correction with new provenance.
                replay = scan.replay(con, s, "trade_terms_csv", DAY, DAY)
                assert replay.ok
                ingest(con, s, 2, note="metadata changed")
                assert store.positions_for(con, DAY).private_inputs_hash == inputs.private_inputs_hash
                run_scheduler(con, s, path, ap)
                assert run_count(con) == before


@pytest.mark.parametrize("path", ["backfill", "eod"])
def test_old_failed_signature_retries_once_and_changed_terms_unlock_failure(env, path, monkeypatch):
    s = env
    ap = Autopilot(settings_loader=lambda: s, today=lambda: DAY)
    attempts = []

    def fail(*args, **kw):
        attempts.append(True)
        raise ValueError("synthetic failure")

    monkeypatch.setattr(cli, "eod_run", fail)
    with store.connect(s.store_url) as con:
        ingest(con, s, 1)
        sig = planner(con, s).todo[0].sig
        if path == "backfill":
            backfill.save_state(s, {"failed": {DAY.isoformat(): sig[:4]}})
            assert planner(con, s).days[0].status == "todo"
        else:
            ap._failed[DAY.isoformat()] = (tuple(sig[0]), *sig[1:4])
        run_scheduler(con, s, path, ap)
        assert len(attempts) == 1
        run_scheduler(con, s, path, ap)
        assert len(attempts) == 1
        ingest(con, s, 2, note="metadata only")
        run_scheduler(con, s, path, ap)
        assert len(attempts) == 1
        ingest(con, s, 3, rate=81)
        run_scheduler(con, s, path, ap)
        assert len(attempts) == 2
        run_scheduler(con, s, path, ap)
        assert len(attempts) == 2


def test_hash_keeps_existing_date_book_and_known_at_scope(env):
    s = env
    with store.connect(s.store_url) as con:
        original = store.positions_for(con, DAY)
        horizon = con.execute("SELECT now()").fetchone()[0]
        ingest(con, s, 1)
        effective = store.positions_for(con, DAY)
        assert effective.private_inputs_hash != original.private_inputs_hash
        assert store.positions_for(con, DAY, known_at=horizon).private_inputs_hash == original.private_inputs_hash
        ingest(con, s, 2, rate=81, book="UNUSED")
        assert store.positions_for(con, DAY).private_inputs_hash == effective.private_inputs_hash
        ingest(con, s, 3, rate=82, day=date(2026, 10, 16))
        assert store.positions_for(con, DAY).private_inputs_hash == effective.private_inputs_hash
        assert store.positions_for(con, date(2026, 10, 16)).private_inputs_hash != effective.private_inputs_hash


def drop(s, name, text):
    """A report file (header line, then rows) into the inbox, then a scan that accepts it."""
    (s.inbox_dirs[0] / name).write_text("﻿" + text.replace("\n", "\r\n"), encoding="utf-8")


@pytest.mark.parametrize("path", ["backfill", "eod"])
def test_ocp_report_or_balances_after_the_run_recompute_the_date(env, path):
    """TDM#181: the OCP report and the cash balances arrive in the evening, after the run; the autopilot (and the
    backfill) run the date again, and the day status names the reason until then."""
    from desk import daystatus

    s = env
    ap = Autopilot(settings_loader=lambda: s, today=lambda: DAY)
    with store.connect(s.store_url) as con:
        cli.eod_run(s, DAY.isoformat(), report_dir=None, out=lambda _: None)
        hash_before = current(con)["private_inputs_hash"]
        for name, text, label, why in (
                ("ocp_20261015.csv", "entity;book_id;ccy;ocp_ccy\nTEST;OFZ;USD;1000\n", "ОВП", "ocp report"),
                ("cash_balances_20261014.csv", "entity;book_id;ccy;account_id;balance_ccy\nTEST;OFZ;USD;A;5\n",
                 "Остатки", "cash balances")):
            old = current(con)
            drop(s, name, text)
            r = scan.scan(con, s)
            assert r.ok and r.files.get("accepted") == 1, r.text()
            assert store.late_inputs(con, DAY, old["positions_known_at"]) != []
            warn = daystatus._rate(daystatus._facts(con, s, DAY, DAY, every=True)[DAY])["warnings"]
            assert any(w.startswith(label) and "после расчёта" in w for w in warn), warn
            assert planner(con, s).days[0].status == "todo" and planner(con, s).days[0].why.startswith(why)
            run_scheduler(con, s, path, ap)
            new = current(con)
            assert new["run_id"] != old["run_id"] and new["private_inputs_hash"] == hash_before  # positions alike
            warn = daystatus._rate(daystatus._facts(con, s, DAY, DAY, every=True)[DAY])["warnings"]
            assert not any("после расчёта" in w for w in warn), warn
            changed = {r["run_id"]: r["changed"] for r in daystatus._runs(con, DAY)}
            assert changed[new["run_id"]] == ["ocp" if label == "ОВП" else "cash_balances"]
            before = run_count(con)
            run_scheduler(con, s, path, ap)
            assert run_count(con) == before and planner(con, s).days[0].status == "done"
        lin = con.execute("SELECT lineage FROM run WHERE run_id = %s", (current(con)["run_id"],)).fetchone()[0]
        assert lin["ocp"]["status"] == "computed"  # the run of the date now reconciles with the report
