"""TDM#570 + #566 end to end on synthetic data: the day's trades (``trades.apply_intraday``) through the published
run
to the header-filter surfaces.

A lot sold to zero by a trade of the day (the report of the day does not list it) is a row at zero that keeps the
PnL of the sale; a position the trades open in a carried book is a row with no input row. Both keep their portfolio:
they are shown under their own portfolio and hidden under another one (the run of the monitor pages
``/concepts-data.js`` and the PnL report ``/api/v1/history/pnl/period``), and in a book of type HTM the portfolio is
HTM, whatever the row says."""
from __future__ import annotations

import json
import math

import pytest
import test_slice_s3
from fastapi.testclient import TestClient
from test_autopilot import cfg
from test_intraday import FRI, MON, at, publish
from test_pnl_new_trades import Day, lineage

from desk import store
from desk.api import create_app
from desk.autopilot import Autopilot

server, stub = test_slice_s3.server, test_slice_s3.stub  # the stub hub's fixtures
S, S_ISIN = "SEC-26238", "RU000A1038V6"
N, N_ISIN = "SEC-29008", "RU000A0JV4P3"
TH = ("trade_id;trade_version;status;trade_date;trade_ts;book_id;position_id;asset_class;"
      "source_instrument_id;"
      "side;quantity;face_amount_ccy;price_pct;accrued_ccy;ccy;instrument_id;isin;x_portfolio\n")


def positions(d: Day, name, rows):
    """A positions file: [(book, position id, instrument, isin, quantity, portfolio)]."""
    (d.tmp / "in").mkdir(exist_ok=True)
    head = ("book_id;position_id;asset_class;source_instrument_id;source_instrument_name;isin;ccy;quantity;"
            "face_amount_ccy;x_portfolio\n")
    (d.tmp / "in" / name).write_text(head + "".join(
        f"{b};{pid};bond;{ins};;{isin};RUB;{q:.0f};{q * 1000:.0f};{pf}\n"
        for b, pid, ins, isin, q, pf in rows),
        encoding="utf-8")


def trade(tid, book, pid, instr, isin, side, qty, price, acc, pf, hhmm="10:30"):
    return (f"{tid};1;live;2026-10-16;2026-10-16T{hhmm}:00;{book};{pid};bond;{instr};{side};{qty};;{price};"
            f"{acc};RUB;"
            f"{instr};{isin};{pf}\n")


def extras(client, run_id, **params):
    js = client.get(f"/concepts-data.js?run_id={run_id}", params=params)
    assert js.status_code == 200, js.text
    return json.loads(js.text[len("window.DESK_EXTRAS = "):-2])


def start_day(hub, tmp_path, store_url, rows):
    """``Day`` of the trades tests, its first intraday run made on our own positions file (with portfolios)."""
    d = Day.__new__(Day)
    d.s = cfg(tmp_path, store_url, hub)
    d.url, d.tmp, d.hub = store_url, tmp_path, hub
    positions(d, "positions_20261016.csv", rows)
    d.clock = {"t": at(FRI, 10, 6)}
    d.ap = Autopilot(settings_loader=lambda: d.s, now=lambda: d.clock["t"])
    publish(hub, FRI, "intraday", "2026-10-16T07:00:00Z", 1)
    d.ap.run_cycle()
    return d


@pytest.fixture
def scenario(stub, tmp_path, store_url):
    """B1: two lots of one bond, in the portfolios A and B (a report of the day follows); B3: a carried trading
    book, all in A; B2: a carried HTM book (its rows say A)."""
    d = start_day(stub, tmp_path, store_url, [
        ("B1", "B1|L1", S, S_ISIN, 12000, "A"), ("B1", "B1|L2", S, S_ISIN, 3000, "B"),
        ("B3", "B3|M1", N, N_ISIN, 20000, "A"), ("B2", "B2|H1", N, N_ISIN, 5000, "A")])
    day_trades = (trade("S1", "B1", "B1|L1", S, S_ISIN, "sell", 12000, 62.0, 10_000.0, "A")
                  + trade("S2", "B2", "B2|H1", N, N_ISIN, "sell", 5000, 99.0, 1_000.0, "A", "10:35")
                  + trade("N1", "B3", "B3|N1", N, N_ISIN, "buy", 100, 59.0, 1_000.0, "A", "10:40"))
    (d.tmp / "in" / "trades_20261016_1100.csv").write_text(TH + day_trades,
                                                           encoding="utf-8")
    d.clock["t"] = at(FRI, 10, 30)
    d.ap.run_cycle()
    publish(d.hub, FRI, "eod_final", "2026-10-19T05:00:00Z", 3)
    d.clock["t"] = at(MON, 8, 30)
    d.ap.run_cycle()
    # the report of the day: no lot L1
    positions(d, "positions_20261019.csv", [("B1", "B1|L2", S, S_ISIN, 3000, "B")])
    d.clock["t"] = at(MON, 8, 50)
    d.ap.run_cycle()
    return d


def period_ids(client, **kw):
    r = client.get("/api/v1/history/pnl/period",
                   params={"from": "2026-10-16", "to": "2026-10-16", "gran": "month", **kw})
    assert r.status_code == 200, r.text
    out = r.json()
    return {q["positionId"]: q for q in out["positions"]}, out["summary"]["total"]


def test_a_lot_sold_to_zero_and_a_position_the_trades_open_keep_their_portfolio(scenario):
    d = scenario
    rid = d.latest_id("EOD")
    with store.connect(d.url) as con:
        rows = {r[0]: r for r in con.execute(
            "SELECT position_id, quantity, (pnl->>'new_trades')::float8 FROM run_position"
            " WHERE run_id = %s", (rid,))}
    # the lots sold out by the trades of the day stay as rows at zero with the PnL of the sale; the opened one has
    # a PnL
    for pid in ("B1|L1", "B2|H1"):
        assert rows[pid][1] == 0.0 and rows[pid][2] != 0.0, pid
    assert rows["B3|N1"][1] == 100.0 and rows["B3|N1"][2] != 0.0
    assert lineage(d.url, rid)["gaps"] == {}
    client = TestClient(create_app(d.url))
    # the HTM book: HTM, not the row's A
    want = {"B1|L1": "A", "B1|L2": "B", "B3|M1": "A", "B3|N1": "A", "B2|H1": "HTM"}
    run = client.get("/api/v1/concepts/run", params={"run_id": rid}).json()
    assert {p["id"]: p["portfolio"] for p in run["positions"]} == want
    everything, _total = period_ids(client)
    assert set(everything) == set(want)
    # under a filter of portfolios: visible under its own, hidden under the other one; the totals follow the
    # rows
    for off in ("A", "B", "C", "HTM", "—"):
        ids, tot = period_ids(client, ports_off=off)
        assert set(ids) == {p for p, pf in want.items() if pf != off}, off
        listed = [q["total"] for q in ids.values() if q["total"] is not None]
        assert tot == pytest.approx(math.fsum(listed)), off
    # the pages' data: the same sums per position as the report (MTD of the run = the report of the month)
    x = extras(client, rid)
    closed = {c["positionId"]: c for c in x["pnlClosed"]}
    # the lot sold out: a closed row, in its portfolio
    assert set(closed) == {"B1|L1"} and closed["B1|L1"]["portfolio"] == "A"
    for pid, p in x["positions"].items():
        assert p["pnlMtd"] == pytest.approx(everything[pid]["total"]), pid
    assert closed["B1|L1"]["mtd"] == pytest.approx(everything["B1|L1"]["total"])
    # the header's MTD (positions and the closed rows through the filter) is the report's total, under every
    # filter
    for off in ("A", "B", "HTM"):
        _ids, tot = period_ids(client, ports_off=off)
        head = math.fsum([p["pnlMtd"] for pid, p in x["positions"].items() if want[pid] != off]
                         + [c["mtd"] for c in x["pnlClosed"] if c["portfolio"] != off])
        assert head == pytest.approx(tot), off
