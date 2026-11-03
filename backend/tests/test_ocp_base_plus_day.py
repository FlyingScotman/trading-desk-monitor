"""The published OCP «report base + day» (``report_base_plus_day``, owner, 03.11.2026, TDM#531; private/1.7) on
synthetic rows: the run on the report's own date (the report without the derivative kinds it gives apart + their PV),
a run inside the next day (a bond's price move and its coupon paid on D, an FX spot trade, the derivatives' PV of D),
what never enters the PV term (an ``other`` equity, a spot, a derivative that matured by B, a kind the report keeps),
the PV whatever ``amount_basis``, the report's nominal as a fallback per kind, Friday's report as Monday's base, a
stale base, a book absent from the report, a unit carried forward, an adapter before 1.7, a split decided per report
file, the sum of the parts, the trades' legs (only after B, one version each, a bond sold out, a forward already in the
positions), the principal and the quantity on each payment date; the 1.7 contract (``ocp.derivatives_ccy`` by kind,
``ocp_policy.published`` ``report_base_plus_day``, ``report_max_age_bd``), the basis by date, and a run end to end.
Every number is invented."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from desk import store
from desk.api import create_app
from desk.inbox import scan
from desk.inbox.reference import _object
from desk.inbox.schema import load_schema, validate_row
from desk.inbox.validate import check_read, check_reference_tables, unit_hash
from desk.publish import ocp_run
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
NOMINAL = replace(POLICY, amount_basis=O.AmountBasis.NOMINAL)


def core_row(pid, book, delta, product="fx_forward", value_date="2026-12-16", nominal=None):
    """A derivative valued by core: its PV per currency (``fx_delta``), its legs' nominal (``ocp_amount``)."""
    return {"position_id": pid, "book_id": book, "instr_type": "FX", "fx_delta": delta,
            "extra": {"core": {"product": product}, "ocp_amount": nominal or delta,
                      "legs": [{"value_date": value_date}]}}


def bond_row(pid="T1|UST", book="T1", mv=1_000_000.0):
    return {"position_id": pid, "book_id": book, "instr_type": "BOND", "ccy": "USD", "mv": mv,
            "fx_delta": {"USD": 15_000.0}, "extra": {"fx_liability": {"state": "computed"}}}


def equity_row(pid="T1|EQ", book="T1", amount=1000.0):
    """A foreign equity at the report's value: component ``other``, not a derivative."""
    return {"position_id": pid, "book_id": book, "instr_type": "EQUITY", "fx_delta": {"USD": amount},
            "extra": {"reported": {"asset_class": "other"}}}


ROWS = [
    core_row("D1|FWD", "D1", {"USD": 490_000.0, "CNY": -3_480_000.0}, nominal={"USD": 500_000.0, "CNY": -3_500_000.0}),
    core_row("D1|OPT", "D1", {"USD": 40_000.0}, "fx_option_vanilla"),  # options: outside the report
    core_row("X1|FWD", "X1", {"USD": 300_000.0}),
    core_row("Z1|FWD", "Z1", {"USD": 70_000.0}),
    bond_row(),
]
FILE = {"file_name": "ocp_20261015.csv", "inbox_file_sha256": "f15"}
UNITS = [{"scope": {"entity": "ENT1", "book_id": "D1"}, "set_id": 11, "valid_date": B, "row_count": 2, **FILE},
         {"scope": {"entity": "ENT1", "book_id": "T1"}, "set_id": 12, "valid_date": B, "row_count": 1, **FILE}]
# the report on B: D1 holds 100k USD of cash; its forward (500k USD / -3.5m CNY) and a CNY future whose position the
# run does not have are apart; T1 holds its bond at its MV on B; the file gives fx_forward and fx_future apart
REPORT = [
    {"entity": "ENT1", "book_id": "D1", "ccy": "USD", "ocp_ccy": "100000",
     "derivatives_ccy": {"fx_forward": "500000", "fx_future": "0"}, "_set_id": 11, "_src": "L2"},
    {"entity": "ENT1", "book_id": "D1", "ccy": "CNY", "ocp_ccy": "0",
     "derivatives_ccy": {"fx_forward": "-3500000", "fx_future": "800000"}, "_set_id": 11, "_src": "L3"},
    {"entity": "ENT1", "book_id": "T1", "ccy": "USD", "ocp_ccy": "1010000",
     "derivatives_ccy": {"fx_forward": "0", "fx_future": "0"}, "_set_id": 12, "_src": "L4"},
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


def test_default_basis_is_report_base_plus_day_from_its_date():
    assert O.PROVISIONAL_POLICY.published is O.PublishedBasis.REPORT_BASE_PLUS_DAY
    assert O.PROVISIONAL_POLICY.report_max_age_bd == 1 and "report_max_age_bd" in O.DECIDED
    assert O.default_published(O.BASIS_SINCE) is O.PublishedBasis.REPORT_BASE_PLUS_DAY
    assert O.default_published(date(2026, 11, 2)) is O.PublishedBasis.REPORT_PLUS_OUTSIDE


def test_run_on_the_base_date_is_the_report_without_derivatives_plus_their_pv():
    r = run()
    p = pub(r)
    d1 = p[("D1", "USD")]
    assert d1["source"] == "report" and d1["base_kind"] == "without_derivatives" and d1["base_date"] == "2026-10-15"
    assert d1["split_kinds"] == ["fx_forward", "fx_future"]
    assert d1["parts"] == {"base": 100_000, "derivatives": 490_000, "bonds": 0, "trades": 0, "outside": 40_000}
    assert d1["amount"] == 630_000 and d1["reported"] == 100_000 and d1["set_id"] == 11
    assert d1["components"] == ["options"] and d1["outside"] == {"options": 40_000}
    base = d1["contributions"]["base"]["items"]
    assert base == [{"kind": "report", "set_id": 11, "valid_date": "2026-10-15", "file": "ocp_20261015.csv",
                     "book_id": "D1", "rows": 1, "amount": 100_000.0}]
    der = d1["contributions"]["derivatives"]["items"]
    assert [(x["position_id"], x["derivative"], x["component"], x["last_flow"]) for x in der] == [
        ("D1|FWD", "fx_forward", "fx_legs", "2026-12-16")]
    t1 = p[("T1", "USD")]  # the same file gives the kinds apart: T1 has none of them
    assert t1["parts"]["base"] == 1_010_000 and t1["amount"] == 1_010_000 and t1["base_kind"] == "without_derivatives"
    # the reconciliation compares the whole report: ocp_ccy + the nominal (zeros kept: the kind is apart)
    c = {(x["book_id"], x["ccy"]): x for x in r.comparisons}
    assert c[("D1", "USD")]["reported"] == 600_000 and c[("D1", "USD")]["reported_base"] == 100_000
    assert c[("D1", "USD")]["reported_derivatives"] == {"fx_forward": 500_000, "fx_future": 0}
    assert c[("D1", "USD")]["split_kinds"] == ["fx_forward", "fx_future"]
    # the nominal against the PV on B: this run's (it is of B), per kind
    chk = {(x["book_id"], x["ccy"], x["kind"]): x for x in r.derivatives_check}
    x = chk[("D1", "USD", "fx_forward")]
    assert (x["nominal"], x["pv"], x["diff"], x["pv_source"], x["over"], x["component"]) == (
        500_000, 490_000, 10_000, "run", True, "fx_legs")
    # what a later run reads as the PV on B: per book, currency and kind
    assert {(x["book_id"], x["ccy"], x["kind"]): x["amount"] for x in r.derivatives_pv}[("D1", "USD", "fx_forward")] \
        == 490_000
    assert_parts_sum(r)


def test_what_never_enters_the_pv_term():
    """Reproduced in review: an ``other`` equity, a core-valued spot and a derivative whose last flow is on or before B
    are in the report's ocp_ccy; adding them again doubled the figure."""
    spot = core_row("T1|SPOT", "T1", {"USD": 500.0}, value_date="2026-10-16")
    matured = core_row("T1|OLD", "T1", {"USD": 700.0}, value_date="2026-10-15")
    terms = [{"trade_id": "S1", "position_id": "T1|SPOT", "product_kind": "fx_forward", "trade_date": "2026-10-14",
              "value_date": "2026-10-16"},
             {"trade_id": "O1", "position_id": "T1|OLD", "product_kind": "fx_forward", "trade_date": "2026-09-01",
              "value_date": "2026-10-15"}]
    rep = [{"entity": "ENT1", "book_id": "T1", "ccy": "USD", "ocp_ccy": "2200",
            "derivatives_ccy": {"fx_forward": "0"}, "_set_id": 12, "_src": "L2"}]
    r = run(rows=[equity_row(), spot, matured], report=rep, units=UNITS[1:], terms=terms)
    t1 = pub(r)[("T1", "USD")]
    assert t1["amount"] == 2200 and t1["parts"]["derivatives"] == 0 and t1["fallback"] == []
    assert O.derivative_kind(equity_row(), []) is None
    assert O.derivative_kind(spot, terms[:1]) is None  # value date within the settlement window: a spot
    assert O.derivative_kind(matured, terms[1:]) == "fx_forward"  # a derivative, but no flow after B
    # the next day, base B: the forward that matured on B is still not added
    assert pub(run(on=D, rows=[matured], report=rep, units=UNITS[1:], terms=terms[1:],
                   days={B: O.BaseDay(B, run_id=1, trade_rows=[])}))[("T1", "USD")]["parts"]["derivatives"] == 0


def test_pv_whatever_the_amount_basis():
    r = run(policy=NOMINAL)
    d1 = pub(r)[("D1", "USD")]
    assert d1["parts"]["derivatives"] == 490_000  # core's FX delta, not the legs' nominal of 500k
    line = next(x for x in r.lines if x.position_id == "D1|FWD" and x.ccy == "USD")
    assert (line.amount, line.pv, line.basis) == (500_000, 490_000, "nominal")  # the reconciliation keeps nominal
    chk = {(x["ccy"], x["kind"]): x for x in r.derivatives_check if x["book_id"] == "D1"}
    assert chk[("USD", "fx_forward")]["pv"] == 490_000


def test_only_the_kinds_the_report_gives_apart_get_pv():
    swap = core_row("D1|SWP", "D1", {"USD": 60_000.0}, "fx_swap")
    r = run(rows=[*ROWS, swap])
    d1 = pub(r)[("D1", "USD")]
    # fx_swap is not in derivatives_ccy: its nominal stays in ocp_ccy, no PV added (a missing mapping is safe)
    assert d1["parts"]["derivatives"] == 490_000
    assert all(i["position_id"] != "D1|SWP" for i in d1["contributions"]["derivatives"]["items"])


def test_fallback_to_the_reports_nominal_per_kind():
    r = run()
    cny = pub(r)[("D1", "CNY")]
    # the forward has its PV, the future has no position in the run: the report's nominal, marked
    assert cny["fallback"] == ["fx_future"]
    assert cny["parts"]["derivatives"] == pytest.approx(-3_480_000 + 800_000)
    nominal = [i for i in cny["contributions"]["derivatives"]["items"] if i["kind"] == "report_nominal"]
    assert nominal == [{"kind": "report_nominal", "derivative": "fx_future", "component": "futures", "set_id": 11,
                        "book_id": "D1", "amount": 800_000.0, "marker": "номинал, PV нет"}]
    g = {x["code"]: x for x in r.gaps}
    assert g["derivatives_nominal_fallback"]["items"] == ["книга D1 CNY: валютные фьючерсы"]
    chk = {(x["ccy"], x["kind"]): x for x in r.derivatives_check if x["book_id"] == "D1"}
    assert chk[("CNY", "fx_future")]["pv"] == 0 and chk[("CNY", "fx_future")]["diff"] == 800_000
    # an NDF apart in the report while NDFs are out of the OCP: its nominal is not put back
    ndf = [{**REPORT[0], "derivatives_ccy": {"fx_forward": "500000", "ndf": "-90000"}}, *REPORT[1:]]
    no_ndf = replace(POLICY, report_components=POLICY.report_components | {"ndf"}, include_ndf=False)
    d1 = pub(run(report=ndf, policy=no_ndf))[("D1", "USD")]
    assert d1["fallback"] == [] and d1["parts"]["derivatives"] == 490_000


def test_intraday_bond_move_coupon_on_d_trade_and_derivatives_pv():
    # on D: the bond's price rose 15k and it paid a 25k coupon (its dirty MV 1,010k -> 1,000k), D1 bought 200k USD
    # spot; the forward's PV is that of D (the run's lines), the PV on B came from B's run
    move = O.BondMove("T1|UST", "T1", "ENT1", "USD", mv_base=1_010_000.0, mv_now=1_000_000.0, paid=25_000.0)
    trades = [{"trade_id": "TR-1", "status": "live", "trade_date": "2026-10-16", "book_id": "D1",
               "position_id": "D1|SPOT", "asset_class": "fx", "side": "buy", "amount_ccy": "200000", "ccy": "USD",
               "settle_date": "2026-10-20"}]
    days = {B: O.BaseDay(B, run_id=7, bonds=[move], pv_base={("D1", "USD", "fx_forward"): 495_000.0},
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
    chk = {(x["book_id"], x["ccy"], x["kind"]): x for x in r.derivatives_check}
    assert chk[("D1", "USD", "fx_forward")]["pv"] == 495_000
    assert chk[("D1", "USD", "fx_forward")]["pv_source"] == "base_run"
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
    assert pub(two)[("D1", "USD")]["source"] == "report" and pub(two)[("D1", "USD")]["base_older"] is True
    assert pub(r)[("D1", "USD")]["base_older"] is False  # Friday's base on Monday: the previous business day
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
           "row_count": 1, "file_name": "ocp_20261014.csv", "inbox_file_sha256": "f14"}
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


def test_split_is_decided_per_report_file_and_kind():
    """T1's unit has no rows (a marker: covered, nothing to report) in a file whose other rows give fx_forward apart:
    T1's forward gets its PV, as the file's. Another file without the field: the whole report, no PV."""
    t1_fwd = core_row("T1|FWD", "T1", {"USD": 80_000.0})
    rep = [REPORT[0]]  # T1's unit (set 12, same file) has no rows
    t1 = pub(run(rows=[*ROWS, t1_fwd], report=rep))[("T1", "USD")]
    assert t1["base_kind"] == "without_derivatives" and t1["parts"]["derivatives"] == 80_000
    other_file = [UNITS[0], {**UNITS[1], "inbox_file_sha256": "g15"}]
    t1 = pub(run(rows=[*ROWS, t1_fwd], report=rep, units=other_file))[("T1", "USD")]
    assert t1["base_kind"] == "with_derivatives" and t1["parts"]["derivatives"] == 0


def test_adapter_before_1_7_keeps_the_nominal_and_adds_no_pv():
    old = [{k: v for k, v in x.items() if k != "derivatives_ccy"} for x in REPORT]
    old[0] = {**old[0], "ocp_ccy": "600000"}  # cash 100k + the forward's nominal 500k, as one figure
    r = run(report=old)
    d1 = pub(r)[("D1", "USD")]
    assert d1["base_kind"] == "with_derivatives" and d1["parts"]["derivatives"] == 0
    assert d1["amount"] == 600_000 + 40_000 and d1["fallback"] == []
    assert {g["code"]: g for g in r.gaps}["report_without_derivatives_split"]["items"] == ["книга D1", "книга T1"]
    assert r.derivatives_check == []  # nothing apart to check


def test_forward_traded_after_b_already_in_positions():
    """A forward D1 traded on D that the positions of D already hold: where the report gives fx_forward apart, its PV
    is in the derivatives and its trade left out. Under a report before 1.7 it is not in the report on B either: new
    since B (not in B's run) its PV is added and the trade left out; not known to be new (no run of B, no terms) its
    trade counts at its nominal. Never lost, never twice."""
    new = core_row("D1|NEW", "D1", {"USD": 99_000.0})
    trades = [{"trade_id": "F9", "status": "live", "trade_date": "2026-10-16", "book_id": "D1",
               "position_id": "D1|NEW", "asset_class": "fx", "side": "buy", "amount_ccy": "100000", "ccy": "USD",
               "settle_date": "2026-12-16"}]
    base_run = {"D1|FWD", "D1|OPT", "T1|UST", "X1|FWD"}
    days = {B: O.BaseDay(B, run_id=7, trade_rows=trades, base_positions=base_run)}
    d1 = pub(run(on=D, rows=[*ROWS, new], days=days, valid_dates={"D1": D}))[("D1", "USD")]
    assert d1["parts"]["derivatives"] == 490_000 + 99_000 and d1["parts"]["trades"] == 0
    old = [{k: v for k, v in x.items() if k != "derivatives_ccy"} for x in REPORT]
    d1 = pub(run(on=D, rows=[*ROWS, new], report=old, days=days, valid_dates={"D1": D}))[("D1", "USD")]
    assert d1["parts"]["derivatives"] == 99_000 and d1["parts"]["trades"] == 0
    assert d1["contributions"]["derivatives"]["items"][0]["note"] == "сделка после базы"
    unknown = {B: O.BaseDay(B, trade_rows=trades)}
    d1 = pub(run(on=D, rows=[*ROWS, new], report=old, days=unknown, valid_dates={"D1": D}))[("D1", "USD")]
    assert d1["parts"]["derivatives"] == 0 and d1["parts"]["trades"] == 100_000


def test_trade_into_a_position_held_before_b_keeps_the_trade_nominal():
    """Reproduced in review: base 200 (an older forward in the report, fx_forward not apart), a trade of 100 after B
    into the same position (FX positions are keyed by book, currency and value date; core rows hold several trades),
    its PV now 295: the whole PV on top of the base gave 495. The position was in B's run: not new, the base keeps the
    older part at its nominal and the after-B trade adds its own nominal: 300."""
    pos = core_row("D1|USD|2026-12-16", "D1", {"USD": 295.0})
    rep = [{"entity": "ENT1", "book_id": "D1", "ccy": "USD", "ocp_ccy": "200", "_set_id": 11, "_src": "L2"}]
    trades = [{"trade_id": "F2", "status": "live", "trade_date": "2026-10-16", "book_id": "D1",
               "position_id": "D1|USD|2026-12-16", "asset_class": "fx", "side": "buy", "amount_ccy": "100",
               "ccy": "USD", "settle_date": "2026-12-16"}]
    days = {B: O.BaseDay(B, run_id=7, trade_rows=trades, base_positions={"D1|USD|2026-12-16"})}
    d1 = pub(run(on=D, rows=[pos], report=rep, units=UNITS[:1], days=days, valid_dates={"D1": D}))[("D1", "USD")]
    assert d1["amount"] == 300 and d1["parts"] == {"base": 200, "derivatives": 0, "bonds": 0, "trades": 100,
                                                   "outside": 0}
    # its terms say every trade is after B: new, whatever B's run held
    terms = [{"trade_id": "F2", "position_id": "D1|USD|2026-12-16", "product_kind": "fx_forward",
              "trade_date": "2026-10-16", "value_date": "2026-12-16"}]
    d1 = pub(run(on=D, rows=[pos], report=rep, units=UNITS[:1], days=days, valid_dates={"D1": D},
                 terms=terms))[("D1", "USD")]
    assert d1["amount"] == 495 and d1["parts"]["trades"] == 0


def test_fx_swap_leg_without_terms_is_an_fx_swap_and_coverage_is_flagged():
    """Reproduced in review: an FX swap leg without terms (product group fx_swap, a value date after the settlement
    window) read as a forward put its notional under fx_forward and the report's fx_swap nominal came back as the
    fallback: 300 -> 600. It is an fx_swap now: its notional, flagged as without PV, no fallback."""
    leg = {"position_id": "D1|SWL", "book_id": "D1", "instr_type": "FX", "fx_delta": {"USD": 300.0},
           "extra": {"reported": {"asset_class": "fx", "value_date": "2026-12-16", "x_product_group": "FX_SWAP"},
                     "fx_delta_basis": "notional (no trade terms: undiscounted)"}}
    rep = [{"entity": "ENT1", "book_id": "D1", "ccy": "USD", "ocp_ccy": "0",
            "derivatives_ccy": {"fx_swap": "300", "fx_forward": "0"}, "_set_id": 11, "_src": "L2"}]
    assert O.derivative_kind(leg, [], POLICY, "fx_legs") == "fx_swap"
    r = run(rows=[leg], report=rep, units=UNITS[:1])
    d1 = pub(r)[("D1", "USD")]
    assert d1["amount"] == 300 and d1["fallback"] == []
    # one position of the kind valued by core, one at its notional: the PV is partial, said in the gaps and the check
    r = run(rows=[leg, core_row("D1|FXS", "D1", {"USD": 495.0}, "fx_swap")],
            report=[{**rep[0], "derivatives_ccy": {"fx_swap": "800"}}], units=UNITS[:1])
    d1 = pub(r)[("D1", "USD")]
    assert d1["amount"] == 795 and d1["partial_pv"] == ["fx_swap"]
    assert {g["code"]: g for g in r.gaps}["derivatives_partial_pv"]["items"] == [
        "книга D1 USD: валютные свопы (1 из 2 позиций без PV)"]
    chk = next(x for x in r.derivatives_check if x["kind"] == "fx_swap")
    assert (chk["positions"], chk["without_pv"], chk["partial"]) == (2, 1, True)


def test_a_swap_started_before_b_still_has_flows_after_it():
    """Reproduced in review: a CCS whose terms' value_date (its start) is before B was taken as finished: its PV of
    4,900 replaced by the report's nominal of 5,000. The last flow is the latest of the start, the maturity and the
    row's maturity. A derivative with no flow date at all is counted and said."""
    ccs = {"position_id": "D1|CCS", "book_id": "D1", "instr_type": "FX", "fx_delta": {"USD": 4900.0},
           "extra": {"core": {"product": "ccs"}}}
    terms = [{"trade_id": "C1", "position_id": "D1|CCS", "product_kind": "ccs", "trade_date": "2026-01-10",
              "value_date": "2026-01-12", "maturity_date": "2029-01-12"}]
    rep = [{"entity": "ENT1", "book_id": "D1", "ccy": "USD", "ocp_ccy": "0", "derivatives_ccy": {"swap": "5000"},
            "_set_id": 11, "_src": "L2"}]
    d1 = pub(run(rows=[ccs], report=rep, units=UNITS[:1], terms=terms))[("D1", "USD")]
    assert d1["parts"]["derivatives"] == 4900 and d1["fallback"] == []
    assert d1["contributions"]["derivatives"]["items"][0]["last_flow"] == "2029-01-12"
    r = run(rows=[ccs], report=rep, units=UNITS[:1])  # no terms, no legs, no maturity: unknown, counted, said
    assert pub(r)[("D1", "USD")]["parts"]["derivatives"] == 4900
    assert {g["code"]: g for g in r.gaps}["derivatives_unknown_last_flow"]["items"] == ["D1|CCS"]


def test_derivative_held_after_its_trade_is_never_counted_twice():
    """Reproduced in review: an NDF traded after B and held in the positions, NDFs outside the report: its trade (100)
    and its position (99, in ``outside``) were both counted. An OCP transfer deal valued as a forward: it is in the
    report's composition (``transfers``), never in the derivatives' PV (-300 published as -600)."""
    ndf = core_row("D1|NDF", "D1", {"USD": 99.0}, "fx_ndf")
    trades = [{"trade_id": "N1", "status": "live", "trade_date": "2026-10-16", "book_id": "D1",
               "position_id": "D1|NDF", "asset_class": "fx", "side": "buy", "amount_ccy": "100", "ccy": "USD",
               "settle_date": "2026-12-16"}]
    rep = [{"entity": "ENT1", "book_id": "D1", "ccy": "USD", "ocp_ccy": "0", "derivatives_ccy": {"fx_forward": "0"},
            "_set_id": 11, "_src": "L2"}]
    d1 = pub(run(on=D, rows=[ndf], report=rep, units=UNITS[:1], days={B: O.BaseDay(B, run_id=7, trade_rows=trades)},
                 valid_dates={"D1": D}))[("D1", "USD")]
    assert d1["amount"] == 99 and d1["parts"]["trades"] == 0 and d1["parts"]["outside"] == 99
    books = {**BOOKS, "D1": {**BOOKS["D1"], "ocp_to": "X1"}}
    xfer = core_row("D1|XFER", "D1", {"USD": -300.0})
    terms = [{"trade_id": "XF", "position_id": "D1|XFER", "product_kind": "fx_forward", "trade_date": "2026-10-01",
              "value_date": "2026-12-16", "counterparty_id": "X1"}]
    rep = [{**rep[0], "ocp_ccy": "-300"}]
    r = O.compute_ocp([xfer], books, B, terms=terms, report_rows=rep, report_units=UNITS[:1], policy=POLICY)
    assert r.per_position["D1|XFER"]["component"] == "transfers"
    d1 = pub(r)[("D1", "USD")]
    assert d1["amount"] == -300 and d1["parts"]["derivatives"] == 0


def test_a_derivative_outside_the_report_is_counted_once_and_has_no_fallback():
    """A derivative of kind ``other`` whose own line is ``fx_legs`` while the report holds ``other`` but not
    ``fx_legs``: in ``outside`` (its line's component), never in the derivatives too. A transfer deal with
    ``transfers`` outside the report: in ``outside``, and the report's nominal of its kind is not put back."""
    odd = core_row("D1|ODD", "D1", {"USD": 70.0}, "exotic")
    pol = replace(POLICY, report_components=(POLICY.report_components | {"other"}) - {"fx_legs"})
    rep = [{"entity": "ENT1", "book_id": "D1", "ccy": "USD", "ocp_ccy": "0", "derivatives_ccy": {"other": "70"},
            "_set_id": 11, "_src": "L2"}]
    assert O.derivative_kind(odd, []) == "other"
    d1 = pub(run(rows=[odd], report=rep, units=UNITS[:1], policy=pol))[("D1", "USD")]
    assert d1["amount"] == 70 and d1["parts"]["outside"] == 70 and d1["parts"]["derivatives"] == 0
    books = {**BOOKS, "D1": {**BOOKS["D1"], "ocp_to": "X1"}}
    xfer = core_row("D1|XFER", "D1", {"USD": -300.0})
    terms = [{"trade_id": "XF", "position_id": "D1|XFER", "product_kind": "fx_forward", "trade_date": "2026-10-01",
              "value_date": "2026-12-16", "counterparty_id": "X1"}]
    rep = [{**rep[0], "derivatives_ccy": {"fx_forward": "-300"}}]
    no_xfer = replace(POLICY, report_components=POLICY.report_components - {"transfers"})
    r = O.compute_ocp([xfer], books, B, terms=terms, report_rows=rep, report_units=UNITS[:1], policy=no_xfer)
    d1 = pub(r)[("D1", "USD")]
    assert d1["amount"] == -300 and d1["fallback"] == [] and d1["parts"]["outside"] == -300


def test_no_position_of_the_kind_with_a_pv_is_said_so():
    leg = {"position_id": "D1|SWL", "book_id": "D1", "instr_type": "FX", "fx_delta": {"USD": 300.0},
           "extra": {"reported": {"asset_class": "fx", "value_date": "2026-12-16", "x_product_group": "fx_swap"},
                     "fx_delta_basis": "notional (no trade terms: undiscounted)"}}
    rep = [{"entity": "ENT1", "book_id": "D1", "ccy": "USD", "ocp_ccy": "0", "derivatives_ccy": {"fx_swap": "300"},
            "_set_id": 11, "_src": "L2"}]
    r = run(rows=[leg], report=rep, units=UNITS[:1])
    d1 = pub(r)[("D1", "USD")]
    assert d1["no_pv"] == ["fx_swap"] and d1["partial_pv"] == []
    assert {g["code"]: g for g in r.gaps}["derivatives_partial_pv"]["items"] == [
        "книга D1 USD: валютные свопы (PV нет ни у одной позиции)"]
    chk = r.derivatives_check[0]
    assert (chk["no_pv"], chk["partial"]) == (True, False)


def test_bond_sold_out_after_b_is_subtracted_once():
    """Reproduced in review: base 1,000 (the bond on B), sold for 1,010 on the 15th, positions of the 15th without it;
    the base of the 14th, the run of the 16th: 1,010 of cash, not 0."""
    b14 = date(2026, 10, 14)
    units = [{"scope": {"entity": "ENT1", "book_id": "T1"}, "set_id": 12, "valid_date": b14, "row_count": 1, **FILE}]
    rep = [{"entity": "ENT1", "book_id": "T1", "ccy": "USD", "ocp_ccy": "1000", "_set_id": 12, "_src": "L2"}]
    sold = O.BondMove("T1|UST", "T1", "ENT1", "USD", mv_base=1000.0, mv_now=None)
    trades = [{"trade_id": "S1", "status": "live", "trade_date": "2026-10-15", "book_id": "T1",
               "position_id": "T1|UST", "asset_class": "bond", "side": "sell", "amount_ccy": "1010", "ccy": "USD",
               "_valid_date": date(2026, 10, 15)}]
    r = run(on=D, rows=[], report=rep, units=units, policy=replace(POLICY, report_max_age_bd=2),
            days={b14: O.BaseDay(b14, run_id=3, bonds=[sold], trade_rows=trades)}, valid_dates={"T1": B})
    t1 = pub(r)[("T1", "USD")]
    assert t1["parts"] == {"base": 1000, "derivatives": 0, "bonds": -1000, "trades": 1010, "outside": 0}
    assert t1["amount"] == 1010
    # positions still of the 14th (before the sale): the bond leaves at the trade's price, the cash comes in
    r = run(on=D, rows=[bond_row(mv=1005.0)], report=rep, units=units, policy=replace(POLICY, report_max_age_bd=2),
            days={b14: O.BaseDay(b14, run_id=3, bonds=[replace(sold, mv_now=1005.0)], trade_rows=trades)},
            valid_dates={"T1": b14})
    assert pub(r)[("T1", "USD")]["amount"] == pytest.approx(1000 + 5 + 1010 - 1010)


def test_trade_legs():
    book_dates = {"T1": D, "D1": D}
    trades = [
        # a USD bond bought, its book's positions dated before the trade: its cash paid and the bond at the price
        {"trade_id": "B1", "status": "live", "trade_date": "2026-10-16", "book_id": "T1", "position_id": "T1|X",
         "asset_class": "bond", "side": "buy", "amount_ccy": "990000", "accrued_ccy": "10000", "ccy": "USD"},
        # an FX forward the positions hold as a derivative: tagged with its kind (left out only where it is apart)
        {"trade_id": "F1", "status": "live", "trade_date": "2026-10-16", "book_id": "D1", "position_id": "D1|FWD2",
         "asset_class": "fx", "side": "sell", "amount_ccy": "100000", "ccy": "USD", "settle_date": "2026-12-16"},
        {"trade_id": "C1", "status": "cancelled", "trade_date": "2026-10-16", "book_id": "D1", "position_id": "D1|S",
         "asset_class": "fx", "side": "buy", "amount_ccy": "1", "ccy": "USD"},
        {"trade_id": "R1", "status": "live", "trade_date": "2026-10-16", "book_id": "T1", "position_id": "T1|OFZ",
         "asset_class": "bond", "side": "buy", "amount_ccy": "1", "ccy": "RUB"},  # a RUB bond: no OCP
        {"trade_id": "O1", "status": "live", "trade_date": "2026-10-16", "book_id": "D1", "position_id": "D1|O",
         "asset_class": "otc", "side": "buy", "amount_ccy": "1", "ccy": "USD"},
        {"trade_id": "U1", "status": "live", "trade_date": "2026-10-16", "book_id": "NEW", "position_id": "NEW|1",
         "asset_class": "fx", "side": "buy", "amount_ccy": "5", "ccy": "USD"},  # a book the reference lacks
    ]
    legs = O.trade_flows(trades, BOOKS, {"T1": B, "D1": D}, {"D1|FWD2": "fx_forward"}, POLICY, after=B)
    assert [(f.trade_id, f.leg, f.amount, f.pv_kind) for f in legs.flows] == [
        ("B1", "cash", -1_000_000, None), ("B1", "security", 1_000_000, None), ("F1", "fx", -100_000, "fx_forward")]
    assert legs.not_valued == ["O1 (otc)"] and legs.unknown_book == ["U1 (NEW)"]
    # the bond's book dated on the trade: its MV is in the bonds' revaluation, only the cash here
    assert [f.leg for f in O.trade_flows(trades[:1], BOOKS, book_dates, {}, POLICY).flows] == ["cash"]
    # cash outside the report: the cash legs come from positions, not from the trades
    no_cash = replace(POLICY, report_components=POLICY.report_components - {"cash"})
    assert [f.leg for f in O.trade_flows(trades[:1], BOOKS, {}, {}, no_cash).flows] == ["security"]
    # a trade of B or before is in the report on B; one version per trade, the highest
    v = {"trade_id": "V1", "status": "live", "book_id": "D1", "position_id": "D1|V", "asset_class": "fx",
         "side": "buy", "ccy": "USD", "settle_date": "2026-10-20"}
    rows = [{**v, "trade_date": "2026-10-15", "amount_ccy": "7", "trade_version": 1, "_valid_date": B},
            {**v, "trade_id": "V2", "trade_date": "2026-10-16", "amount_ccy": "1", "trade_version": 1,
             "_valid_date": D},
            {**v, "trade_id": "V2", "trade_date": "2026-10-16", "amount_ccy": "3", "trade_version": 2,
             "_valid_date": D}]
    assert [(f.trade_id, f.amount) for f in O.trade_flows(rows, BOOKS, {}, {}, POLICY, after=B).flows] == [("V2", 3)]
    gaps = {g["code"]: g for g in run(on=D, days={B: O.BaseDay(B, run_id=1, trade_rows=trades)},
                                      valid_dates={"T1": B, "D1": D}).gaps}
    assert gaps["trades_unknown_book"]["items"] == ["U1 (NEW)"]


def test_coupons_and_principal_on_the_quantity_held_each_payment_date():
    """1,000 bonds held on B; on the 16th 400 sold; a coupon of 10 per bond and an amortisation of 100 per bond paid
    on the 19th: (10 + 100) x 600; a coupon on the 16th itself before the sale counts on 1,000. The FX liability's
    coupons of the day are not principal: the schedule is used whenever the terms are known."""
    terms = SimpleNamespace(
        coupons=(SimpleNamespace(pay_date=date(2026, 10, 16), amount_per_bond=2.0),
                 SimpleNamespace(pay_date=date(2026, 10, 19), amount_per_bond=10.0),
                 SimpleNamespace(pay_date=date(2026, 10, 30), amount_per_bond=10.0)),
        redemptions=(SimpleNamespace(date=date(2026, 10, 19), kind="amortization", value_per_bond=100.0),))
    before = {"position_id": "T1|UST", "book_id": "T1", "ccy": "USD", "mv": 1e6,
              "extra": {"quantity_bonds": 1000.0}}
    now = {**before, "extra": {"fx_liability": {"state": "computed", "reference": "prev_dirty_mv",
                                                "reference_date": "2026-10-15", "coupons_ccy": 999.0}}}
    sale = [{"trade_id": "S", "status": "live", "trade_date": "2026-10-16", "side": "sell", "quantity": "400"}]
    paid, why = ocp_run.paid_since(now, before, terms, B, date(2026, 10, 19), sale)
    assert paid == pytest.approx(2.0 * 1000 + (10.0 + 100.0) * 600) and why == ""
    # no terms: the FX liability's coupons of the day, said
    paid, why = ocp_run.paid_since(now, before, None, B, D)
    assert paid == 999.0 and why == ocp_run.NO_TERMS_COUPONS


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


# ---- the contract, private/1.7 -------------------------------------------------------------------------------


def test_contract_1_7_ocp_row_and_policy():
    row = {"_src": "L2", "entity": "ENT1", "book_id": "D1", "ccy": "usd", "ocp_ccy": 100000,
           "derivatives_ccy": {"fx_forward": -1.5, "fx_swap": "0", "ndf": None}}
    norm, errs, _ = validate_row("ocp", row)
    assert errs == [] and norm["derivatives_ccy"] == {"fx_forward": "-1.5", "fx_swap": "0"}  # a zero is kept
    assert O.derivative_nominals(norm["derivatives_ccy"]) == {"fx_forward": -1.5, "fx_swap": 0.0}
    assert set(O.DERIVATIVE_KINDS) == set(load_schema("ocp")["properties"]["derivatives_ccy"]["properties"])
    for bad, code in (({"fx_legs": "1"}, "bad_value"), ({"fx_forward": "1e5"}, "bad_number"),
                      ("fx_forward=1", "bad_value")):
        assert [(e.code, e.field) for e in validate_row("ocp", {**row, "derivatives_ccy": bad})[1]] == [
            (code, "derivatives_ccy")]
    # canonical: in the unit's change hash; {} differs from absent (an adapter before 1.7)
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
    # the reference adapter's cell: key=amount pairs; a key twice: the last value, a warning
    assert _object("fx_forward=-1000000,ndf=0") == ({"fx_forward": "-1000000", "ndf": "0"}, False)
    assert _object("fx_forward=1,fx_forward=2") == ({"fx_forward": "2"}, True)
    assert _object("{}") == ({}, False) and _object("fx_forward") == ("fx_forward", False)
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


def test_basis_follows_the_date_unless_the_adapter_gives_it(store_url):
    """A date before 03.11.2026 whose ocp_policy gives no basis keeps report_plus_outside (recomputed later, it keeps
    the basis it was published on); from that date the engine's report_base_plus_day; a row giving it wins."""
    with store.connect(store_url) as con:
        now = datetime.now().astimezone()
        old, src, _ = ocp_run.run_policy(con, date(2026, 10, 15), now)
        assert old.published is O.PublishedBasis.REPORT_PLUS_OUTSIDE and "до 03.11.2026" in src["published"]
        new, src, _ = ocp_run.run_policy(con, date(2026, 11, 3), now)
        assert new.published is O.PublishedBasis.REPORT_BASE_PLUS_DAY and src["published"].startswith("OQ-18")
    assert ocp_run.COMPUTED_TEXT[O.PublishedBasis.REPORT_BASE_PLUS_DAY] != ocp_run.STATUS_TEXT[ocp_run.COMPUTED]


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
              "ENT1;D1;USD;150000;;;fx_forward=260000,ndf=0\nENT1;T1;;;;;\n")
    trades = ("trade_id;trade_version;status;trade_date;book_id;position_id;asset_class;source_instrument_id;side;"
              "amount_ccy;ccy;settle_date\nTR-1;1;live;2026-10-16;D1;D1|SPOT2;fx;USDRUB_TOM;buy;200000;USD;2026-10-19\n")
    cfg, inbox = tmp_path / "cfg", tmp_path / "in"
    cfg.mkdir()
    inbox.mkdir()
    # the adapter gives the basis (the dates of the fixtures are before the engine's default changed)
    policy = ("policy_id;valid_from;published;report_max_age_bd\nfirm;;report_base_plus_day;1\n")
    for name, text in (("books.csv", BOOKS_CSV), ("entities.csv", ENTITIES), ("funding_rules.csv", RULES),
                       ("ocp_policy.csv", policy)):
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
    assert ob["text"] == ocp_run.COMPUTED_TEXT[O.PublishedBasis.REPORT_BASE_PLUS_DAY]
    assert lb["split_kinds"] == ["fx_forward", "ndf"]
    assert lb["parts"]["base"] == 150_000 and lb["parts"]["derivatives"] == pytest.approx(250_000)
    assert lb["parts"]["trades"] == 0 and lb["amount"] == pytest.approx(400_000)
    rec = {(x["book_id"], x["ccy"]): x for x in ob["reconciliation"]}[("D1", "USD")]
    assert rec["reported"] == 410_000 and rec["reported_base"] == 150_000 and rec["split"]
    chk = [x for x in ob["derivatives_check"] if x["book_id"] == "D1"]
    assert [(x["kind"], x["nominal"], x["pv"], x["pv_source"]) for x in chk] == [
        ("fx_forward", 260_000, pytest.approx(250_000), "run")]
    assert {(x["book_id"], x["kind"]): x["amount"] for x in ob["derivatives_pv"]}[("D1", "fx_forward")] \
        == pytest.approx(250_000)
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


def test_pv_on_a_base_published_on_another_basis_is_unknown(store_url):
    """The first base day after the switch: B's run was published on report_plus_outside, its block has no
    derivatives' PV of its own (``derivatives_pv`` []). Not a PV of 0 (every check row a diff of the whole nominal,
    WARN lines): unknown, «PV на базу неизвестен»."""
    from test_ocp_history import insert

    with store.connect(store_url) as con:
        old = insert(con, "2026-10-15", ocp={"version": 3, "status": "computed", "derivatives_pv": [],
                                             "published": {"basis": "report_plus_outside", "by_book": []}})
        new = insert(con, "2026-10-14", ocp={"version": 3, "status": "computed", "derivatives_pv": [
            {"book_id": "D1", "ccy": "USD", "kind": "fx_forward", "amount": 495.0}],
            "published": {"basis": "report_base_plus_day", "by_book": []}})
        assert ocp_run._pv_base(con, old) is None
        assert ocp_run._pv_base(con, new) == {("D1", "USD", "fx_forward"): 495.0}
    r = run(on=D, days={B: O.BaseDay(B, run_id=old, pv_base=None, trade_rows=[])})
    chk = {(x["book_id"], x["ccy"], x["kind"]): x for x in r.derivatives_check}[("D1", "USD", "fx_forward")]
    assert (chk["pv"], chk["diff"], chk["over"], chk["note"]) == (None, None, False, "PV на базу неизвестен")
