"""The published OCP «report base + day» (``report_base_plus_day``, owner, 03.11.2026, TDM#531; private/1.7) on
synthetic rows: the run on the report's own date (the report without derivatives + their PV), a run inside the next
day (a bond's price move and its coupon paid on D, an FX spot trade, the derivatives' PV of D), the report's nominal as
a fallback where no PV line exists, Friday's report as Monday's base, a stale base, a book absent from the report, an
adapter before 1.7, the sum of the parts, the trades' legs; the 1.7 contract (``ocp.derivatives_ccy``,
``ocp_policy.published`` ``report_base_plus_day``, ``report_max_age_bd``) and a run end to end. Every number is
invented."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import store
from desk.api import create_app
from desk.inbox import scan
from desk.inbox.schema import validate_row
from desk.inbox.validate import check_read, check_reference_tables, unit_hash
from desk.rules import ocp as O
from desk.rules.reference import apply_ocp_policy
from desk.settings import Settings

B = date(2026, 10, 15)  # Thursday: the report's date
D = date(2026, 10, 16)  # Friday: the run inside the day after it
BOOKS = {
    "D1": {"book_id": "D1", "book_type": "DERIVS", "entity": "ENT1", "ocp_to": None},
    "T1": {"book_id": "T1", "book_type": "TRADING", "entity": "ENT1", "ocp_to": None},
    "X1": {"book_id": "X1", "book_type": "DERIVS", "entity": "ENT1", "ocp_to": None},  # no row in the report
    "Z1": {"book_id": "Z1", "book_type": "DERIVS", "entity": "ENT2", "ocp_to": None},  # its entity is not reported
}
# the report holds everything but options and NDFs (an invented composition)
POLICY = O.OcpPolicy(report_components=frozenset(set(O.COMPONENTS) - {"options", "ndf"}))


def core_row(pid, book, delta, product="fx_forward"):
    return {"position_id": pid, "book_id": book, "instr_type": "FX", "fx_delta": delta,
            "extra": {"core": {"product": product}, "ocp_amount": delta}}


def bond_row(pid="T1|UST", book="T1", mv=1_000_000.0):
    return {"position_id": pid, "book_id": book, "instr_type": "BOND", "ccy": "USD", "mv": mv,
            "fx_delta": {"USD": 15_000.0}, "extra": {"fx_liability": {"state": "computed"}}}


ROWS = [
    core_row("D1|FWD", "D1", {"USD": 490_000.0, "CNY": -3_480_000.0}),  # PV of a USD/CNY forward
    core_row("D1|OPT", "D1", {"USD": 40_000.0}, "fx_option_vanilla"),  # options: outside the report
    core_row("X1|FWD", "X1", {"USD": 300_000.0}),
    core_row("Z1|FWD", "Z1", {"USD": 70_000.0}),
    bond_row(),
]
UNITS = [{"scope": {"entity": "ENT1", "book_id": "D1"}, "set_id": 11, "valid_date": B, "row_count": 2,
          "file_name": "ocp_20261015.csv"},
         {"scope": {"entity": "ENT1", "book_id": "T1"}, "set_id": 12, "valid_date": B, "row_count": 1,
          "file_name": "ocp_20261015.csv"}]
# the report on B: D1 holds 100k USD of cash and its forward apart (its nominal 500k USD / -3.5m CNY) plus a CNY
# future whose position the run does not have; T1 holds its bond at its MV on B
REPORT = [
    {"entity": "ENT1", "book_id": "D1", "ccy": "USD", "ocp_ccy": "100000", "derivatives_ccy": {"fx_legs": "500000"},
     "_set_id": 11, "_src": "L2"},
    {"entity": "ENT1", "book_id": "D1", "ccy": "CNY", "ocp_ccy": "0",
     "derivatives_ccy": {"fx_legs": "-3500000", "futures": "800000"}, "_set_id": 11, "_src": "L3"},
    {"entity": "ENT1", "book_id": "T1", "ccy": "USD", "ocp_ccy": "1010000", "_set_id": 12, "_src": "L2"},
]


def run(on=B, report=REPORT, units=UNITS, rows=ROWS, policy=POLICY, days=None, **kw):
    return O.compute_ocp(list(rows), BOOKS, on, report_rows=report, report_units=units, policy=policy, days=days,
                         **kw)


def pub(r):
    return {(x["book_id"], x["ccy"]): x for x in r.published}


def assert_parts_sum(r):
    """Every published line: its parts sum to its amount, each part's contributions (and their rest) to the part."""
    for x in r.published:
        if x.get("parts") is None:
            continue
        assert math.fsum(x["parts"].values()) == pytest.approx(x["amount"], abs=1e-6)
        for p, v in x["parts"].items():
            c = (x["contributions"] or {}).get(p)
            got = math.fsum([*(i["amount"] for i in c["items"]), (c["rest"] or {}).get("amount", 0.0)]) if c else 0.0
            assert got == pytest.approx(v, abs=1e-6), (x["book_id"], x["ccy"], p)


def test_default_basis_is_report_base_plus_day():
    assert O.PROVISIONAL_POLICY.published is O.PublishedBasis.REPORT_BASE_PLUS_DAY
    assert O.PROVISIONAL_POLICY.report_max_age_bd == 1 and "report_max_age_bd" in O.DECIDED


def test_run_on_the_base_date_is_the_report_without_derivatives_plus_their_pv():
    r = run()
    p = pub(r)
    d1 = p[("D1", "USD")]
    assert d1["source"] == "report" and d1["base_kind"] == "without_derivatives" and d1["base_date"] == "2026-10-15"
    assert d1["parts"] == {"base": 100_000, "derivatives": 490_000, "bonds": 0, "trades": 0, "outside": 40_000}
    assert d1["amount"] == 630_000 and d1["reported"] == 100_000 and d1["set_id"] == 11
    assert d1["components"] == ["options"] and d1["outside"] == {"options": 40_000}
    base = d1["contributions"]["base"]["items"]
    assert base == [{"kind": "report", "set_id": 11, "valid_date": "2026-10-15", "file": "ocp_20261015.csv",
                     "book_id": "D1", "rows": 1, "amount": 100_000.0}]
    assert d1["contributions"]["derivatives"]["items"][0]["position_id"] == "D1|FWD"
    t1 = p[("T1", "USD")]  # a report without derivatives_ccy on its unit: the whole report (no derivatives in it)
    assert t1["parts"]["base"] == 1_010_000 and t1["amount"] == 1_010_000 and t1["base_kind"] == "with_derivatives"
    # the reconciliation compares the whole report: ocp_ccy + the nominal
    c = {(x["book_id"], x["ccy"]): x for x in r.comparisons}
    assert c[("D1", "USD")]["reported"] == 600_000 and c[("D1", "USD")]["reported_base"] == 100_000
    assert c[("D1", "USD")]["reported_derivatives"] == {"fx_legs": 500_000} and c[("D1", "USD")]["split"]
    # the nominal against the PV on B: this run's lines (it is of B)
    chk = {(x["book_id"], x["ccy"], x["component"]): x for x in r.derivatives_check}
    x = chk[("D1", "USD", "fx_legs")]
    assert (x["nominal"], x["pv"], x["diff"], x["pv_source"], x["over"]) == (500_000, 490_000, 10_000, "run", True)
    assert_parts_sum(r)


def test_fallback_to_the_reports_nominal_without_a_pv_line():
    r = run()
    cny = pub(r)[("D1", "CNY")]
    # the forward has its PV, the future has no position in the run: the report's nominal, marked
    assert cny["fallback"] == ["futures"]
    assert cny["parts"]["derivatives"] == pytest.approx(-3_480_000 + 800_000)
    nominal = [i for i in cny["contributions"]["derivatives"]["items"] if i["kind"] == "report_nominal"]
    assert nominal == [{"kind": "report_nominal", "component": "futures", "set_id": 11, "book_id": "D1",
                        "amount": 800_000.0, "marker": "номинал, PV нет"}]
    g = {x["code"]: x for x in r.gaps}
    assert g["derivatives_nominal_fallback"]["items"] == ["книга D1 CNY: фьючерсы"]
    chk = {(x["ccy"], x["component"]): x for x in r.derivatives_check if x["book_id"] == "D1"}
    assert chk[("CNY", "futures")]["pv"] == 0 and chk[("CNY", "futures")]["diff"] == 800_000


def test_intraday_bond_move_coupon_on_d_trade_and_derivatives_pv():
    # on D: the bond's price rose 15k and it paid a 25k coupon (its dirty MV 1,010k -> 1,000k), D1 bought 200k USD
    # spot; the forward's PV is that of D (the run's lines), the PV on B came from B's run
    move = O.BondMove("T1|UST", "T1", "ENT1", "USD", mv_base=1_010_000.0, mv_now=1_000_000.0, paid=25_000.0)
    trades = [{"trade_id": "TR-1", "status": "live", "trade_date": "2026-10-16", "book_id": "D1",
               "position_id": "D1|SPOT", "asset_class": "fx", "side": "buy", "amount_ccy": "200000", "ccy": "USD",
               "settle_date": "2026-10-20"}]
    days = {B: O.BaseDay(B, run_id=7, bonds=[move], pv_base={("D1", "USD", "fx_legs"): 495_000.0},
                         trade_rows=trades)}
    r = run(on=D, days=days, valid_dates={"D1": B, "T1": B})
    p = pub(r)
    t1 = p[("T1", "USD")]
    assert t1["parts"] == {"base": 1_010_000, "derivatives": 0, "bonds": 15_000, "trades": 0, "outside": 0}
    assert t1["contributions"]["bonds"]["items"] == [{"kind": "position", "position_id": "T1|UST", "book_id": "T1",
                                                      "mv_base": 1_010_000.0, "mv_now": 1_000_000.0, "paid": 25_000.0,
                                                      "amount": 15_000.0}]
    d1 = p[("D1", "USD")]
    assert d1["parts"] == {"base": 100_000, "derivatives": 490_000, "bonds": 0, "trades": 200_000, "outside": 40_000}
    assert d1["contributions"]["trades"]["items"][0] == {
        "kind": "trade", "trade_id": "TR-1", "book_id": "D1", "leg": "fx", "trade_date": "2026-10-16",
        "amount": 200_000.0}
    assert d1["base_date"] == "2026-10-15" and d1["valid_date"] == B
    chk = {(x["book_id"], x["ccy"], x["component"]): x for x in r.derivatives_check}
    assert chk[("D1", "USD", "fx_legs")]["pv"] == 495_000 and chk[("D1", "USD", "fx_legs")]["pv_source"] == "base_run"
    assert not {"trades_not_loaded", "no_base_run", "base_older"} & {g["code"] for g in r.gaps}
    assert_parts_sum(r)
    # the same day without the trades loaded or B's run: said, nothing invented
    bare = run(on=D, days={B: O.BaseDay(B)})
    g = {x["code"]: x for x in bare.gaps}
    assert g["trades_not_loaded"]["items"] == ["книга D1", "книга T1", "книга X1"]
    assert g["no_base_run"]["items"][0] == "книга D1: 15.10.2026"
    assert pub(bare)[("T1", "USD")]["parts"]["bonds"] == 0


def test_friday_report_is_mondays_base_and_older_is_stale():
    fri = [{**u, "valid_date": date(2026, 10, 16)} for u in UNITS]
    mon = date(2026, 10, 19)
    r = run(on=mon, units=fri, days={date(2026, 10, 16): O.BaseDay(date(2026, 10, 16), run_id=1, trade_rows=[])})
    assert {x["source"] for x in r.published if x["book_id"] in ("D1", "T1")} == {"report"}
    assert "stale_report" not in {g["code"] for g in r.gaps}
    # Thursday's report on Monday: two business days, the limit is one: derived, said
    old = run(on=mon)
    p = pub(old)
    assert p[("D1", "USD")]["source"] == "derived_stale_report" and "parts" not in p[("D1", "USD")]
    assert p[("D1", "USD")]["amount"] == pytest.approx(490_000 + 40_000)  # all of D1 from positions
    stale = [g for g in old.gaps if g["code"] == "stale_report"]
    assert len(stale) == 2 and "старше 1 раб. дн." in stale[0]["text"]
    # a holiday on Monday (the run's calendar): Friday's report is Tuesday's base
    tue = date(2026, 10, 20)
    hol = run(on=tue, units=fri, is_business_day=lambda d: d.weekday() < 5 and d != mon,
              days={date(2026, 10, 16): O.BaseDay(date(2026, 10, 16), run_id=1, trade_rows=[])})
    assert pub(hol)[("D1", "USD")]["source"] == "report"
    # the adapter may admit an older base: then its date is said
    two = run(on=mon, policy=replace(POLICY, report_max_age_bd=2), days={B: O.BaseDay(B, run_id=1, trade_rows=[])})
    assert pub(two)[("D1", "USD")]["source"] == "report"
    assert {g["code"]: g for g in two.gaps}["base_older"]["items"][0] == "книга D1 от 15.10.2026"


def test_book_of_a_covered_entity_absent_from_the_report_has_base_zero():
    r = run()
    p = pub(r)
    x1 = p[("X1", "USD")]
    assert (x1["source"], x1["base_kind"], x1["set_id"], x1["reported"]) == ("report", "book_absent", None, 0)
    assert x1["parts"] == {"base": 0, "derivatives": 300_000, "bonds": 0, "trades": 0, "outside": 0}
    # an entity without any report unit: derived, said
    z1 = p[("Z1", "USD")]
    assert z1["source"] == "derived" and z1["amount"] == 70_000
    assert {g["code"]: g for g in r.gaps}["books_outside_report"]["items"] == ["Z1"]
    assert any("X1" in n and "база 0" in n for n in r.notes)


def test_unit_carried_forward_from_before_the_base_is_zero_in_the_base():
    """X1 is in the report of the 14th but not in the report of the 15th: its unit of the 14th is still in force
    (``covers`` ``listed`` carries it forward), yet the report on B is a full snapshot of ENT1: X1's base is 0, never
    its old figure, and the gaps name the unit."""
    old = {"scope": {"entity": "ENT1", "book_id": "X1"}, "set_id": 9, "valid_date": date(2026, 10, 14),
           "row_count": 1, "file_name": "ocp_20261014.csv"}
    rows = [*REPORT, {"entity": "ENT1", "book_id": "X1", "ccy": "USD", "ocp_ccy": "5000000",
                      "derivatives_ccy": {}, "_set_id": 9, "_src": "L2"}]
    r = run(report=rows, units=[*UNITS, old])
    x1 = pub(r)[("X1", "USD")]
    assert (x1["base_kind"], x1["set_id"], x1["base_date"]) == ("book_absent", None, "2026-10-15")
    assert x1["parts"]["base"] == 0 and x1["amount"] == 300_000  # its forward's PV only
    g = {x["code"]: x for x in r.gaps}
    assert g["carried_forward_unit"]["items"] == ["книга X1: последняя строка отчёта от 14.10.2026, в базу не входит"]
    assert "stale_report" not in g  # not «derived from positions»: it is simply not in the base
    # the reconciliation still compares the unit as it is in force; REPORT_PLUS_OUTSIDE keeps publishing it
    assert any(c["book_id"] == "X1" and c["set_id"] == 9 for c in r.comparisons)
    ro = run(report=rows, units=[*UNITS, old], policy=replace(POLICY, published=O.PublishedBasis.REPORT_PLUS_OUTSIDE,
                                                              report_max_age_days=1))
    assert pub(ro)[("X1", "USD")]["reported"] == 5_000_000


def test_adapter_before_1_7_keeps_the_nominal_and_adds_no_pv():
    old = [{k: v for k, v in x.items() if k != "derivatives_ccy"} for x in REPORT]
    old[0] = {**old[0], "ocp_ccy": "600000"}  # cash 100k + the forward's nominal 500k, as one figure
    r = run(report=old)
    d1 = pub(r)[("D1", "USD")]
    assert d1["base_kind"] == "with_derivatives" and d1["parts"]["derivatives"] == 0
    assert d1["amount"] == 600_000 + 40_000 and d1["fallback"] == []
    assert {g["code"]: g for g in r.gaps}["report_without_derivatives_split"]["items"] == ["книга D1", "книга T1"]
    assert r.derivatives_check == []  # nothing apart to check


def test_parts_sum_to_the_amount_and_contributions_are_bounded():
    rows = [*ROWS, *(core_row(f"D1|F{i:02d}", "D1", {"USD": 1000.0 + i}) for i in range(25))]
    r = run(rows=rows)
    d1 = pub(r)[("D1", "USD")]
    der = d1["contributions"]["derivatives"]
    assert len(der["items"]) == O.CONTRIBUTIONS_MAX and der["rest"]["count"] == 26 - O.CONTRIBUTIONS_MAX
    assert der["items"][0]["position_id"] == "D1|FWD"  # the largest first
    assert d1["parts"]["derivatives"] == pytest.approx(490_000 + sum(1000.0 + i for i in range(25)))
    assert_parts_sum(r)
    by = O.published_by_currency(r.published)["USD"]
    assert by["by_part"]["derivatives"] == pytest.approx(d1["parts"]["derivatives"] + 300_000)
    assert by["total"] == pytest.approx(sum(x["amount"] for x in r.published if x["ccy"] == "USD"))


def test_report_plus_outside_and_derived_keep_working():
    old = replace(POLICY, published=O.PublishedBasis.REPORT_PLUS_OUTSIDE)
    r = run(policy=old)
    d1 = pub(r)[("D1", "USD")]
    # the report's whole figure (ocp_ccy + its derivatives' nominal) + the components outside it, as before 1.7
    assert d1["amount"] == 600_000 + 40_000 and d1["reported"] == 600_000 and "parts" not in d1
    assert ("X1", "USD") in pub(r) and pub(r)[("X1", "USD")]["source"] == "derived"
    assert r.derivatives_check == []
    derived = run(policy=replace(POLICY, published=O.PublishedBasis.DERIVED))
    assert {x["source"] for x in derived.published} == {"derived"}


def test_trade_legs():
    held = {"T1|NEW": (D, "bonds"), "D1|FWD2": (D, "fx_legs")}
    trades = [
        # a USD bond bought, not in the run's positions yet: its cash paid and the bond at the price
        {"trade_id": "B1", "status": "live", "trade_date": "2026-10-16", "book_id": "T1", "position_id": "T1|X",
         "asset_class": "bond", "side": "buy", "amount_ccy": "990000", "accrued_ccy": "10000", "ccy": "USD"},
        # bought and already in the positions: only the cash (its MV is in the bonds' revaluation)
        {"trade_id": "B2", "status": "live", "trade_date": "2026-10-16", "book_id": "T1", "position_id": "T1|NEW",
         "asset_class": "bond", "side": "buy", "amount_ccy": "500000", "ccy": "USD"},
        # an FX forward the positions hold as a derivative: in their PV, not here; one they do not: its nominal
        {"trade_id": "F1", "status": "live", "trade_date": "2026-10-16", "book_id": "D1", "position_id": "D1|FWD2",
         "asset_class": "fx", "side": "sell", "amount_ccy": "100000", "ccy": "USD", "settle_date": "2026-12-16"},
        {"trade_id": "F2", "status": "live", "trade_date": "2026-10-16", "book_id": "D1", "position_id": "D1|FWD3",
         "asset_class": "fx", "side": "sell", "amount_ccy": "70000", "ccy": "USD", "settle_date": "2026-12-16"},
        {"trade_id": "C1", "status": "cancelled", "trade_date": "2026-10-16", "book_id": "D1", "position_id": "D1|S",
         "asset_class": "fx", "side": "buy", "amount_ccy": "1", "ccy": "USD"},
        {"trade_id": "R1", "status": "live", "trade_date": "2026-10-16", "book_id": "T1", "position_id": "T1|OFZ",
         "asset_class": "bond", "side": "buy", "amount_ccy": "1", "ccy": "RUB"},  # a RUB bond: no OCP
        {"trade_id": "O1", "status": "live", "trade_date": "2026-10-16", "book_id": "D1", "position_id": "D1|O",
         "asset_class": "otc", "side": "buy", "amount_ccy": "1", "ccy": "USD"},
    ]
    flows, skipped = O.trade_flows(trades, BOOKS, held, POLICY)
    got = [(f.trade_id, f.leg, f.ccy, f.amount) for f in flows]
    assert got == [("B1", "cash", "USD", -1_000_000), ("B1", "security", "USD", 1_000_000),
                   ("B2", "cash", "USD", -500_000), ("F2", "fx", "USD", -70_000)]
    assert skipped == ["O1 (otc)"]
    # cash outside the report: the cash legs come from positions, not from the trades
    no_cash = replace(POLICY, report_components=POLICY.report_components - {"cash"})
    assert [f.leg for f in O.trade_flows(trades[:1], BOOKS, held, no_cash)[0]] == ["security"]


# ---- the contract, private/1.7 -------------------------------------------------------------------------------


def test_contract_1_7_ocp_row_and_policy():
    row = {"_src": "L2", "entity": "ENT1", "book_id": "D1", "ccy": "usd", "ocp_ccy": 100000,
           "derivatives_ccy": {"fx_legs": -1.5, "futures": "250000", "ndf": None}}
    norm, errs, _ = validate_row("ocp", row)
    assert errs == [] and norm["derivatives_ccy"] == {"fx_legs": "-1.5", "futures": "250000"}
    for bad, code in (({"bonds": "1"}, "bad_value"), ({"fx_legs": "1e5"}, "bad_number"), ("fx_legs=1", "bad_value")):
        assert [(e.code, e.field) for e in validate_row("ocp", {**row, "derivatives_ccy": bad})[1]] == [
            (code, "derivatives_ccy")]
    # canonical: in the unit's change hash; {} (none on the row) differs from absent (an adapter before 1.7)
    plain = {k: v for k, v in norm.items() if k != "derivatives_ccy"}
    assert len({unit_hash([norm]), unit_hash([plain]), unit_hash([{**plain, "derivatives_ccy": {}}])}) == 3
    # a unit of a 1.7 adapter and one of a 1.6 adapter (no field) are both accepted
    decl = {"source": "s", "dataset": "ocp", "time_basis": "end_of_day", "completeness": "full_snapshot",
            "scope": ["entity", "book_id"], "covers": "listed"}
    hit = {"source": "s", "report_date": "2026-10-15", "date_from": "name", "report_ts": None, "seq": None}
    books = [{"book_id": "D1", "name": "D1", "desk": "FX", "book_type": "DERIVS", "entity": "ENT1", "active": True}]
    for r, contract in ((row, "private/1.7"), ({k: v for k, v in row.items() if k != "derivatives_ccy"},
                                                "private/1.6")):
        c = check_read(decl, hit, {"rows": [r], "scopes": [{"entity": "ENT1", "book_id": "D1"}], "issues": [],
                                   "controls": {}, "meta": {}}, books=books, contract=contract)
        assert c.status == "accepted" and c.issues == [], c.issues
    # ocp_policy: the new basis and the age in business days
    pol = {"_src": "L2", "policy_id": "firm", "published": "report_base_plus_day", "report_max_age_bd": "2"}
    norm, errs, _ = validate_row("ocp_policy", pol)
    assert errs == [] and norm["report_max_age_bd"] == 2
    s = apply_ocp_policy([norm])
    assert s.policy.published is O.PublishedBasis.REPORT_BASE_PLUS_DAY and s.policy.report_max_age_bd == 2
    assert s.source["report_max_age_bd"] == "ocp_policy:firm"
    neg = apply_ocp_policy([{**norm, "report_max_age_bd": -1}])
    assert neg.policy.report_max_age_bd == 1 and neg.conflicts
    _, issues = check_reference_tables({"books": [{**books[0], "_src": "L2"}],
                                             "ocp_policy": [{**pol, "report_max_age_bd": "-1"}]})
    assert [i["code"] for i in issues] == ["bad_policy_value"]


# ---- a run end to end ------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ctx():
    from desk.marketdata.bundle import SnapshotDir
    from desk.orchestrator import prepare

    return prepare(SnapshotDir(Path(__file__).parent / "fixtures" / "mdhub_v1"), B.isoformat())


@pytest.fixture(autouse=True)
def _fresh_process():
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


def test_run_on_the_base_date_then_inside_the_next_day(store_url, ctx, tmp_path):
    """The reference adapter's files: positions, the report of the 15th with D1's forward apart (private/1.7), a USD
    spot trade of D1 on the 16th. The run of the 15th publishes the report without the forward + its PV; the run of
    the 16th adds the trade, its base the report of the 15th, the PV on B from the run of the 15th."""
    from test_ocp_run import BOOKS as BOOKS_CSV
    from test_ocp_run import CASH, ENTITIES, POSITIONS, RULES, TERMS, _csv

    from desk.orchestrator import value
    from desk.publish import desk_run
    from desk.valuation.positions import merge, plan

    report = ("entity;book_id;ccy;ocp_ccy;ocp_rub;rate_rub_per_unit;derivatives_ccy\n"
              "ENT1;D1;USD;150000;;;fx_legs=260000\nENT1;T1;;;;;\n")
    trades = ("trade_id;trade_version;status;trade_date;book_id;position_id;asset_class;source_instrument_id;side;"
              "amount_ccy;ccy;settle_date\nTR-1;1;live;2026-10-16;D1;D1|SPOT2;fx;USDRUB_TOM;buy;200000;USD;2026-10-19\n")
    cfg, inbox = tmp_path / "cfg", tmp_path / "in"
    cfg.mkdir()
    inbox.mkdir()
    for name, text in (("books.csv", BOOKS_CSV), ("entities.csv", ENTITIES), ("funding_rules.csv", RULES)):
        _csv(cfg / name, text)
    for name, text in (("positions_20261016.csv", POSITIONS), ("trade_terms_20261015.csv", TERMS),
                       ("cash_balances_20261015.csv", CASH), ("ocp_20261015.csv", report),
                       ("trades_20261016_1100.csv", trades)):
        _csv(inbox / name, text)
    s = Settings(snapshot_cache=Path(__file__).parent / "fixtures" / "mdhub_v1", inbox_dirs=(inbox,),
                 inbox_archive=tmp_path / "archive", private_adapter=None, private_adapter_config=cfg,
                 inbox_poll_seconds=0, inbox_lookback_days=0)
    with store.connect(store_url) as con:
        res = scan.scan(con, s)
        assert res.ok and res.files == {"accepted": 5}, res.text()
        rids = []
        for on in (B, D):
            inp = store.positions_for(con, on)
            bp = plan(ctx, inp)
            pos = merge(bp, value(ctx, bp.requests, position_ids=bp.position_ids))
            rid, st, _ = desk_run.publish(con, ctx, pos, prev=None, inputs=inp, business_date=on)
            assert st == "complete"
            rids.append(rid)
    c = TestClient(create_app(store_url))
    ob, od = (c.get(f"/api/v1/runs/{rid}/ocp").json() for rid in rids)
    # the run of the 15th: the report without the forward (150k) + the forward's PV (no terms: its notional, 250k)
    lb = {(x["book_id"], x["ccy"]): x for x in ob["published"]["by_book"]}[("D1", "USD")]
    assert ob["published"]["basis"] == "report_base_plus_day" and lb["base_kind"] == "without_derivatives"
    assert lb["parts"]["base"] == 150_000 and lb["parts"]["derivatives"] == pytest.approx(250_000)
    assert lb["parts"]["trades"] == 0 and lb["amount"] == pytest.approx(400_000)
    rec = {(x["book_id"], x["ccy"]): x for x in ob["reconciliation"]}[("D1", "USD")]
    assert rec["reported"] == 410_000 and rec["reported_base"] == 150_000 and rec["split"]
    chk = [x for x in ob["derivatives_check"] if x["book_id"] == "D1"]
    assert [(x["component"], x["nominal"], x["pv"], x["pv_source"]) for x in chk] == [
        ("fx_legs", 260_000, pytest.approx(250_000), "run")]
    # the run of the 16th: the base of the 15th + the forward's PV + the spot trade of the 16th
    ld = {(x["book_id"], x["ccy"]): x for x in od["published"]["by_book"]}[("D1", "USD")]
    assert ld["base_date"] == "2026-10-15" and ld["parts"]["trades"] == 200_000
    assert ld["amount"] == pytest.approx(600_000)
    assert ld["contributions"]["trades"]["items"][0]["trade_id"] == "TR-1"
    chk = [x for x in od["derivatives_check"] if x["book_id"] == "D1"]
    assert chk[0]["pv_source"] == "base_run" and chk[0]["pv_date"] == "2026-10-15"
    assert "trades_not_loaded" not in {g["code"] for g in od["gaps"]}
    hist = c.get("/api/v1/history/ocp", params={"run_id": rids[1]}).json()
    day = {x["date"]: x for x in hist["days"]}["2026-10-16"]
    line = next(x for x in day["lines"] if x["book"] == "D1" and x["ccy"] == "USD")
    assert line["baseDate"] == "2026-10-15" and line["parts"]["trades"] == 200_000 and line["setId"]
