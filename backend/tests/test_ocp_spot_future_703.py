"""TDM#703 (private contract 1.9): the ``ocp`` row's field ``spot_future``, the report's lines of a spot type with a future
value date, is part of the contract (schema, validation, change hash, reference adapter, conformance) and the monitor reads
it before the ``x_spot_future`` an adapter 0.9.14 gave (TDM#693). Being canonical, it is in the unit's hash: a report accepted
without it and read again with an adapter that gives it is a new revision, the date's run is stale (``late_inputs``) and the
OCP is computed again, all from the interface (the button that reads the reports again, ``/api/v1/inbox/replay``), no command
line. Synthetic data, invented numbers."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_ocp_run import BOOKS, CASH, ENTITIES, POSITIONS, RULES, TERMS, _csv
from test_ocp_spot_future_693 import POLICY as POLICY_693
from test_ocp_spot_future_693 import line, run

from desk import adapter_jobs, backfill, store
from desk.api.app import create_app
from desk.inbox import conformance, scan
from desk.inbox.schema import validate_row
from desk.inbox.spi import CONTRACT
from desk.inbox.validate import unit_hash
from desk.rules import ocp as O
from desk.settings import load

B = date(2026, 10, 15)
SNAPS = Path(__file__).parent / "fixtures" / "mdhub_v1_4"  # with the FX inputs: core values the forward (a PV)
ROW = {"_src": "L2", "entity": "ENT1", "book_id": "D1", "ccy": "usd", "ocp_ccy": "400000"}
LINE = {"value_date": "2026-12-17", "pair": "USD/RUB", "amount_ccy": 250000, "src": "R5"}


def ok(**kw):
    norm, errs, dropped = validate_row("ocp", {**ROW, **kw})
    assert errs == [] and dropped == 0, errs
    return norm


def bad(value):
    _, errs, _ = validate_row("ocp", {**ROW, "spot_future": value})
    return [(e.code, e.field) for e in errs]


# ---- the contract: schema, validation, hash ----------------------------------------------------------------------


def test_the_contract_is_1_9_and_the_field_is_in_the_schema() -> None:
    assert CONTRACT == "private/1.9"
    row = ok(spot_future=[LINE])
    assert row["spot_future"] == [{"value_date": "2026-12-17", "pair": "USD/RUB", "amount_ccy": "250000", "src": "R5"}]
    assert row["ccy"] == "USD"


def test_entries_are_normalised_in_a_fixed_order_and_an_empty_list_is_the_field_absent() -> None:
    a = {"value_date": "2026-12-17", "amount_ccy": "1.50"}
    b = {"value_date": "2026-12-14", "amount_ccy": -5000.0, "pair": "EUR/USD"}
    assert ok(spot_future=[a, b])["spot_future"] == ok(spot_future=[b, a])["spot_future"]
    assert ok(spot_future=[a])["spot_future"] == [{"value_date": "2026-12-17", "amount_ccy": "1.5"}]  # amounts as text
    assert ok(spot_future=[b])["spot_future"][0]["amount_ccy"] == "-5000"
    assert "spot_future" not in ok(spot_future=[]) and "spot_future" not in ok(spot_future=None)


@pytest.mark.parametrize("value,code", [
    ("x", "bad_value"), ({"value_date": "2026-12-17", "amount_ccy": "1"}, "bad_value"), ([None], "bad_value"),
    (["x"], "bad_value"), ([{"amount_ccy": "1"}], "bad_value"), ([{"value_date": "2026-12-17"}], "bad_value"),
    ([{"value_date": "2026-12-17", "amount_ccy": "1", "other": "x"}], "bad_value"),
    ([{"value_date": "2026-12-17", "amount_ccy": "1", "pair": ""}], "bad_value"),
    ([{"value_date": "17.12.2026", "amount_ccy": "1"}], "bad_date"),
    ([{"value_date": "2026-12-17", "amount_ccy": "1e3"}], "bad_number"),
    ([{"value_date": "2026-12-17", "amount_ccy": True}], "bad_number")])
def test_a_bad_entry_is_an_error_of_the_field(value, code) -> None:
    assert bad(value) == [(code, "spot_future")]


def test_the_field_is_in_the_change_hash_the_x_field_is_not() -> None:
    plain, std = ok(), ok(spot_future=[LINE])
    legacy = {**ok(), "x_spot_future": [LINE]}
    assert unit_hash([plain]) == unit_hash([legacy])  # what an adapter 0.9.14 gave never made a revision
    assert unit_hash([plain]) != unit_hash([std])
    assert unit_hash([std]) == unit_hash([{**ok(spot_future=[LINE]), "x_spot_future": [LINE]}])
    other = {**LINE, "amount_ccy": 250001}
    assert unit_hash([std]) != unit_hash([ok(spot_future=[other])])
    assert unit_hash([plain]) == unit_hash([ok(spot_future=[])])  # no lines: no new revision of a report read again


# ---- the monitor reads the field, and still the x_ one ------------------------------------------------------------


def _report(**kw):
    return {"entity": "ENT1", "book_id": "D1", "ccy": "EUR", "ocp_ccy": "1000000", "_set_id": 11, "_src": "L2",
            "derivatives_ccy": {"ndf": "0"}, **kw}


def _terms():
    return [{"trade_id": "T-1", "position_id": "D1|NDF", "product_kind": "fx_forward", "book_id": "D1", "ndf": True,
             "base_ccy": "EUR", "quote_ccy": "RUB", "side": "buy", "notional_base": 800_000, "notional_quote": 80_000_000,
             "value_date": "2026-12-18", "fixing_date": "2026-12-16"}]


def _pos():
    return [{"position_id": "D1|NDF", "book_id": "D1", "instr_type": "FX", "fx_delta": {"EUR": 790_000.0},
             "extra": {"core": {"product": "fx_ndf", "trades": ["D1|NDF"]}, "ocp_amount": {"EUR": 800_000.0},
                       "legs": [{"value_date": "2026-12-18", "fixing_date": "2026-12-16"}]}}]


def _published(rep):
    r = run(_pos(), [rep], _terms())
    return {(x["book_id"], x["ccy"]): x for x in r.published}[("D1", "EUR")]


def test_the_contract_field_and_the_x_field_give_the_same_ocp_and_the_contract_field_wins() -> None:
    entry = line("2026-12-14", "800000")
    std = _published(_report(spot_future=[entry]))
    old = _published(_report(x_spot_future=[entry]))
    none = _published(_report())
    assert std["amount"] == old["amount"] and std["parts"] == old["parts"] and std["amount"] != none["amount"]
    assert std["spot_future"]["out"] and old["spot_future"]["out"] and "spot_future" not in none  # the line is taken out
    # both on a row: the field of the contract decides (a line nobody matches in x_ is not read)
    far = line("2027-03-01", "123")
    both = _published(_report(spot_future=[entry], x_spot_future=[far]))
    assert both["parts"] == std["parts"] and len(both["spot_future"]["out"]) == 1 and not both["spot_future"]["unmatched"]
    assert O._spot_future_of({"x_spot_future": [far]}) == [far] and O._spot_future_of({"x": {"x_spot_future": [far]}}) == [far]
    assert O._spot_future_of({"spot_future": [entry], "x_spot_future": [far]}) == [entry]
    assert O._spot_future_of({"ccy": "EUR"}) is None


def test_both_fields_with_the_same_lines_are_not_added_together() -> None:
    """The transition: an adapter of 1.9 gives `spot_future` and `x_spot_future` alike, for a monitor before 1.9 (it drops
    the first and reads the second). The monitor of 1.9 reads the contract field only: the line is taken out once."""
    entry = line("2026-12-14", "800000")
    std = _published(_report(spot_future=[entry]))
    both = _published(_report(spot_future=[entry], x_spot_future=[entry]))
    assert both["parts"] == std["parts"] and both["amount"] == std["amount"]
    assert len(both["spot_future"]["out"]) == 1 and not both["spot_future"]["unmatched"]
    row = ok(spot_future=[LINE])
    assert unit_hash([{**row, "x_spot_future": [LINE]}]) == unit_hash([row])  # the x_ copy changes no revision


def test_a_report_without_the_field_is_as_before() -> None:  # an adapter 1.8
    r = run([], [_report()], [], policy=POLICY_693)
    x = {(x["book_id"], x["ccy"]): x for x in r.published}[("D1", "EUR")]
    assert "spot_future" not in x and x["parts"]["base"] == 1_000_000


# ---- the reference adapter and the conformance kit -----------------------------------------------------------------


OCP_1_9 = """entity;book_id;ccy;ocp_ccy;ocp_rub;rate_rub_per_unit;derivatives_ccy;spot_future
ENT1;D1;USD;400000;;;fx_forward=0;2026-12-17,USD/RUB,250000,R5|2026-12-14,,-5000
ENT1;D1;EUR;10;;;;[]
ENT1;T1;;;;;;
"""


def test_the_reference_adapter_reads_the_field_and_the_kit_passes_contract_1_9(tmp_path: Path) -> None:
    d = tmp_path / "in"
    d.mkdir()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    _csv(cfg / "books.csv", BOOKS)
    _csv(d / "ocp_20261015.csv", OCP_1_9)
    rep = conformance.check(None, [str(d)], stub_ctx=True, config_dir=str(cfg))
    assert rep.ok, rep.text()
    g = rep.groups[("ocp_csv", "2026-10-15")]
    assert g.statuses == {"accepted": 1} and g.issues == {} and g.rows == 2, (g.statuses, g.issues, g.rows)
    from desk.inbox import reference

    rows = reference.read(str(d / "ocp_20261015.csv"), {"source": "ocp_csv", "report_date": "2026-10-15"}, None)["rows"]
    assert rows[0]["spot_future"] == [
        {"value_date": "2026-12-17", "pair": "USD/RUB", "amount_ccy": "250000", "src": "R5"},
        {"value_date": "2026-12-14", "amount_ccy": "-5000"}] and rows[1]["spot_future"] == []
    # a malformed cell is text: the backend says bad_value, the whole unit is not taken
    _csv(d / "ocp_20261015.csv", OCP_1_9.replace("250000,R5", "250000,R5,extra,more").replace("-5000", "-5000"))
    rep = conformance.check(None, [str(d)], stub_ctx=True, config_dir=str(cfg))
    assert rep.groups[("ocp_csv", "2026-10-15")].units.get("rejected") == 1, rep.text()


# ---- a report accepted without the field, read again with it ------------------------------------------------------

ADAPTER = r'''import json, os
from desk.inbox import reference as R

SPI_VERSION = 1


def describe():
    return R.describe()


def match(path, ctx):
    return R.match(path, ctx)


def _mode(ctx):
    p = os.path.join(ctx.config_dir or "", "emit.json")
    return json.load(open(p, encoding="utf-8")).get("mode") if os.path.isfile(p) else "none"


def read(path, hit, ctx):
    out = R.read(path, hit, ctx)
    mode = _mode(ctx)
    if hit["source"] == "ocp_csv":
        for row in out["rows"]:  # the report's lines come from the file; what the adapter gives of them is its version
            lines = row.pop("spot_future", None)
            if lines and mode == "std":
                row["spot_future"] = lines  # an adapter 1.9
            elif lines and mode == "x":
                row["x_spot_future"] = lines  # an adapter 0.9.14
    return out


def reference(ctx):
    return R.reference(ctx)
'''
FWD_TERMS = TERMS + ("FW-1;;1;D1;ENT1;D1|FWD;fx_forward;FWD;2026-10-01;2026-12-17;USD;RUB;buy;250000;20500000;82;0;"
                     "CP1\n")
POLICY = "policy_id;valid_from;published;report_max_age_bd\nfirm;;report_base_plus_day;1\n"
# D1's report in USD: the cash 150k, the forward's nominal 250k written as a spot line with the forward's value date
# (the base holds both), the forward's kind apart with no nominal
REPORT = """entity;book_id;ccy;ocp_ccy;ocp_rub;rate_rub_per_unit;derivatives_ccy;spot_future
ENT1;D1;USD;400000;;;fx_forward=0;2026-12-17,USD/RUB,250000,R5
ENT1;T1;;;;;;
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    h = tmp_path / "home"
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    pkg = tmp_path / "spot_adapter_t"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(ADAPTER, encoding="utf-8")
    cfg, inbox = tmp_path / "cfg", tmp_path / "in"
    cfg.mkdir()
    inbox.mkdir()
    for name, text in (("books.csv", BOOKS), ("entities.csv", ENTITIES), ("funding_rules.csv", RULES),
                       ("ocp_policy.csv", POLICY)):
        _csv(cfg / name, text)
    (cfg / "emit.json").write_text(json.dumps({"mode": "none"}), encoding="utf-8")  # an adapter 1.8
    for name, text in (("positions_20261016.csv", POSITIONS), ("trade_terms_20261015.csv", FWD_TERMS),
                       ("cash_balances_20261015.csv", CASH), ("ocp_20261015.csv", REPORT)):
        _csv(inbox / name, text)
    old = time.time() - 3600  # written an hour ago: stable for the first scan
    for f in inbox.iterdir():
        os.utime(f, (old, old))
    h.mkdir()
    (h / "settings.json").write_text(json.dumps({
        "private_adapter": str(pkg), "private_adapter_config": str(cfg), "inbox_dirs": [{"path": str(inbox)}],
        "inbox_archive": str(tmp_path / "archive"), "inbox_lookback_days": 0, "snapshot_cache": str(SNAPS)}),
        encoding="utf-8")
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    scan.forget_adapters()
    adapter_jobs.reset()
    yield h
    adapter_jobs.reset()
    scan.forget_adapters()


@pytest.fixture(scope="module")
def ctx():
    from desk.marketdata.bundle import SnapshotDir
    from desk.orchestrator import prepare

    return prepare(SnapshotDir(SNAPS), B.isoformat())


def _install(home: Path, mode: str) -> None:
    """What installing an adapter of another version does to the reports: the adapter gives other rows from the same files."""
    cfg = Path(load().private_adapter_config)
    (cfg / "emit.json").write_text(json.dumps({"mode": mode}), encoding="utf-8")
    scan.forget_adapters()
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


def _run(store_url: str, ctx) -> tuple[int, dict]:
    from desk.orchestrator import value
    from desk.publish import desk_run
    from desk.valuation.positions import merge, plan

    with store.connect(store_url) as con:
        inp = store.positions_for(con, B)
        bp = plan(ctx, inp)
        pos = merge(bp, value(ctx, bp.requests, position_ids=bp.position_ids))
        rid, st, _ = desk_run.publish(con, ctx, pos, prev=None, inputs=inp, business_date=B)
        assert st == "complete"
    return rid, TestClient(create_app(store_url)).get(f"/api/v1/runs/{rid}/ocp").json()


def _d1(out: dict) -> dict:
    return {(x["book_id"], x["ccy"]): x for x in out["published"]["by_book"]}[("D1", "USD")]


def _wait(c: TestClient, timeout: float = 90.0) -> dict:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = c.get("/api/v1/inbox/replay").json()
        if not r["running"]:
            return r
        time.sleep(0.2)
    raise AssertionError("the replay is still running")


def _revisions(store_url: str) -> list[tuple[int, str]]:
    with store.connect(store_url) as con:
        return con.execute("SELECT revision, unit_hash FROM dataset_set WHERE dataset = 'ocp' AND scope_key LIKE '%D1%' ORDER BY set_id").fetchall()


def test_a_report_accepted_without_the_field_is_recomputed_after_it_is_read_again_with_it(
        home: Path, store_url: str, ctx) -> None:
    st = load()
    with store.connect(store_url) as con:
        res = scan.scan(con, st)
        assert res.ok and res.files.get("accepted") == 4, res.text()
    c = TestClient(create_app(store_url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    first = _revisions(store_url)
    assert len(first) == 1

    # before: an adapter 1.8 (no field): the base holds the forward's nominal, and the position's PV is added on top
    _, before = _run(store_url, ctx)
    d1 = _d1(before)
    assert d1["parts"]["base"] == 400_000 and not d1.get("spot_future")
    with store.connect(store_url) as con:
        run_row = store.default_runs(con, B, B)[B]
        assert store.late_inputs(con, B, run_row["positions_known_at"]) == []

    # the adapter of 1.9 is installed; the new scan does not touch the accepted file (it is not offered again)
    _install(home, "std")
    with store.connect(store_url) as con:
        assert scan.scan(con, st).files == {} and len(_revisions(store_url)) == 1

    # the owner reads the reports again: the button of «Перечитать отчёты» (POST /api/v1/inbox/replay)
    src = next(s for s in c.get("/api/v1/inbox/replay").json()["sources"] if s["source"] == "ocp_csv")
    assert src["latest"] == "2026-10-15"
    r = c.post("/api/v1/inbox/replay", json={"source": "ocp_csv", "from": "2026-10-15", "to": "2026-10-15"})
    assert r.status_code == 202, r.text
    j = _wait(c)["job"]
    assert j["state"] == "done" and j["result"]["revisions"] == 1 and j["result"]["files"] == {"accepted": 1}, j
    second = _revisions(store_url)
    assert len(second) == 2 and second[1][1] != first[0][1]  # a new revision of the unit: the field is in its hash

    # the run of the date is stale for the backfill (and the autopilot), by the existing rule
    with store.connect(store_url) as con:
        run_row = store.default_runs(con, B, B)[B]
        assert store.late_inputs(con, B, run_row["positions_known_at"]) == ["ocp"]
        assert backfill.late_why(con, B, run_row) == "ocp report changed after the run"

    # after: the line is taken out of the base and the deal stands in its place
    _, after = _run(store_url, ctx)
    d1 = _d1(after)
    assert d1["parts"]["base"] == 150_000 and len(d1["spot_future"]["out"]) == 1, d1["spot_future"]
    assert d1["spot_future"]["out"][0]["position_id"] == "D1|FWD" and not d1["spot_future"]["unmatched"]
    assert d1["amount"] < _d1(before)["amount"]

    # the same reading again: no new revision, nothing to recompute
    r = c.post("/api/v1/inbox/replay", json={"source": "ocp_csv", "from": "2026-10-15", "to": "2026-10-15"})
    assert r.status_code == 202
    assert _wait(c)["job"]["result"]["revisions"] == 0 and len(_revisions(store_url)) == 2


def test_an_adapter_0_9_14_and_one_of_1_8_work_as_before(home: Path, store_url: str, ctx) -> None:
    """The x_ field (adapter 0.9.14) takes the lines out as it did, with no revision of its own; a report of an adapter
    without lines (1.8, or 1.9 with none) is read again into no new revision."""
    st = load()
    _install(home, "x")
    with store.connect(store_url) as con:
        assert scan.scan(con, st).files.get("accepted") == 4
    _, out = _run(store_url, ctx)
    d1 = _d1(out)
    assert d1["parts"]["base"] == 150_000 and len(d1["spot_future"]["out"]) == 1  # TDM#693 as it was
    # the adapter is updated to 1.9, nothing else changes: reading again gives a revision (the field is canonical now)
    _install(home, "std")
    with store.connect(store_url) as con:
        res = scan.replay(con, st, "ocp_csv", B, B)
        assert res.ok and res.revisions == 1
    _, out = _run(store_url, ctx)
    assert _d1(out)["parts"] == d1["parts"] and _d1(out)["amount"] == d1["amount"]  # the same OCP from the field


def test_a_report_without_lines_is_read_again_into_no_new_revision(home: Path, store_url: str) -> None:
    """The report of the date has no line of a spot type: read again by an adapter 1.8, or 1.9 giving the field (an empty
    list) or not giving it, it is the same revision, so nothing is recomputed for it."""
    st = load()
    f = Path(st.inbox_dirs[0]) / "ocp_20261015.csv"
    _csv(f, REPORT.replace("2026-12-17,USD/RUB,250000,R5", "[]"))
    old = time.time() - 3600
    os.utime(f, (old, old))
    _install(home, "std")
    with store.connect(store_url) as con:
        assert scan.scan(con, st).files.get("accepted") == 4
    assert len(_revisions(store_url)) == 1
    for mode in ("std", "none", "x", "std"):
        _install(home, mode)
        with store.connect(store_url) as con:
            res = scan.replay(con, st, "ocp_csv", B, B)
        assert res.ok and res.files == {"accepted": 1} and res.revisions == 0, mode
    assert len(_revisions(store_url)) == 1
