"""The OCP engine (``desk.rules.ocp``, wave B2, TDM#59) on synthetic rows in the shape of the run's content: the
components, the reconciliation with a report at book and entity level with its explanation by component and hints,
the transfers, and the provisional conventions (OQ-11 nominal basis and NDFs, report components, DEC06)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from desk.api.compat import ocp_transfers
from desk.rules import ocp as O
from desk.rules.funding import FundingLiability

ON = date(2026, 10, 15)  # a Thursday: two business days later is Monday the 19th
BOOKS = {
    "T1": {"book_id": "T1", "book_type": "TRADING", "entity": "ENT1", "ocp_to": None},
    "D1": {"book_id": "D1", "book_type": "DERIVS", "entity": "ENT1", "ocp_to": "X1"},
    "X1": {"book_id": "X1", "book_type": "DERIVS", "entity": "ENT1", "ocp_to": None},
    "F1": {"book_id": "F1", "book_type": "FUTURES", "entity": "ENT2", "ocp_to": None},
}


def fx_row(pid, book, delta, value_date=None, deal_id=None, **extra):
    rep = {"asset_class": "fx", **({"value_date": value_date} if value_date else {}),
           **({"deal_id": deal_id} if deal_id else {})}
    return {"position_id": pid, "book_id": book, "instr_type": "FX", "fx_delta": delta,
            "extra": {"reported": rep, **extra}}


def core_row(pid, book, delta, product, ocp_amount=None, trades=None):
    return {"position_id": pid, "book_id": book, "instr_type": "FX", "fx_delta": delta,
            "extra": {"core": {"product": product, "trades": trades}, "ocp_amount": ocp_amount or delta}}


ROWS = [
    {"position_id": "T1|BOND", "book_id": "T1", "instr_type": "BOND", "ccy": "USD", "fx_delta": {"USD": 1_000_500.0}},
    {"position_id": "T1|OFZ", "book_id": "T1", "instr_type": "BOND", "ccy": "RUB"},
    fx_row("D1|SPOT", "D1", {"USD": 200_000.0}, "2026-10-19"),  # spot: to be settled
    fx_row("D1|NOTERMS", "D1", {"USD": 30_000.0}, "2026-11-20", fx_delta_basis="notional"),  # a forward, no terms
    core_row("D1|FWD", "D1", {"USD": 490_000.0}, "fx_forward", {"USD": 500_000.0}),
    core_row("D1|NDF", "D1", {"USD": -100_000.0}, "fx_ndf"),
    core_row("F1|FUT", "F1", {"CNY": 1_000_000.0}, "fx_future"),
    fx_row("D1|XFER", "D1", {"USD": -620_000.0}, "2026-10-16", deal_id="XF-1"),
    fx_row("X1|XFER", "X1", {"USD": 620_000.0}, "2026-10-16", deal_id="XF-2"),
]
TERMS = [
    {"trade_id": "XF-1", "book_id": "D1", "product_kind": "fx_forward", "value_date": "2026-10-16",
     "contract_rate": "81.25", "counterparty_id": "X1"},
    {"trade_id": "XF-2", "book_id": "X1", "product_kind": "fx_forward", "value_date": "2026-10-16",
     "contract_rate": "81.25", "counterparty_id": "D1"},
    {"trade_id": "OTHER", "book_id": "D1", "position_id": "D1|FWD", "counterparty_id": "CP-A"},
]
CASH = [{"entity": "ENT1", "book_id": "D1", "ccy": "USD", "account_id": "MAIN", "balance_ccy": "50000"},
        {"entity": "ENT1", "book_id": "D1", "ccy": "RUB", "account_id": "MAIN", "balance_ccy": "-1000000"}]
LIAB = [FundingLiability("T1|BOND", "T1", "ENT1", "USD", -1_000_000.0)]
UNITS = [{"scope": {"entity": "ENT1", "book_id": "D1"}, "set_id": 1, "valid_date": ON, "row_count": 1},
         {"scope": {"entity": "ENT1", "book_id": "T1"}, "set_id": 2, "valid_date": ON, "row_count": 0}]


def report(usd):
    return [{"entity": "ENT1", "book_id": "D1", "ccy": "USD", "ocp_ccy": str(usd), "_set_id": 1, "_src": "L2"}]


GROSS = O.OcpPolicy(bond_funding_liability=True)
"""ROWS hold the bond gross (a run before TDM#54): its liability as the ``funding`` component."""


def run(report_rows, policy=GROSS, units=UNITS):
    return O.compute_ocp(ROWS, BOOKS, ON, terms=TERMS, cash=CASH, liabilities=LIAB, report_rows=report_rows,
                         report_units=units, policy=policy)


def test_lines_by_component():
    r = run(report(0))
    comp = {k: v["component"] for k, v in r.per_position.items()}
    assert comp == {"T1|BOND": "bonds", "D1|SPOT": "cash", "D1|NOTERMS": "fx_legs", "D1|FWD": "fx_legs",
                    "D1|NDF": "ndf", "F1|FUT": "futures", "D1|XFER": "transfers", "X1|XFER": "transfers"}
    assert "T1|OFZ" not in r.per_position  # a RUB row has no OCP
    d = O.derive(r.lines)
    assert d[("ENT1", "D1", "USD")] == pytest.approx({"cash": 250_000, "fx_legs": 520_000, "ndf": -100_000,
                                                      "transfers": -620_000})
    assert d[("ENT1", "T1", "USD")] == pytest.approx({"bonds": 1_000_500, "funding": -1_000_000})
    assert d[("ENT2", "F1", "CNY")] == {"futures": 1_000_000}
    assert not any(x.ccy == "RUB" for x in r.lines)  # the RUB cash balance: no OCP
    assert {x.basis for x in r.lines if x.position_id == "D1|NOTERMS"} == {"notional"}
    by = O.by_currency(r.lines)
    assert by["USD"]["transfers"] == 0  # a transfer moves the OCP between books: the desk's total keeps it
    assert sum(by["USD"].values()) == pytest.approx(670_000 + 500)


def test_reconciliation_by_book_explained_by_components():
    # the default: the report holds every component, so the report without the FX legs' PV is a break that the
    # hint attributes to that component
    r = run(report(-470_000))
    c = {(x["book_id"], x["ccy"]): x for x in r.comparisons}
    d1 = c[("D1", "USD")]
    assert d1["derived"] == pytest.approx(50_000) and d1["in_report"] == pytest.approx(50_000)
    assert d1["break"] == pytest.approx(-520_000) and d1["over"]
    assert any("PV валютных ног" in h and "не включает" in h for h in d1["hints"])
    t1 = c[("T1", "USD")]  # a covered book without rows: reported 0; bonds net of their liability are within tolerance
    assert t1["reported"] == 0 and not t1["in_report_file"] and t1["break"] == pytest.approx(-500) and not t1["over"]
    assert r.outside_report == [  # books no report unit covers: listed, not breaks
        {"entity": "ENT1", "book_id": "X1", "ccy": "USD", "derived": 620_000.0, "components": {"transfers": 620_000.0}},
        {"entity": "ENT2", "book_id": "F1", "ccy": "CNY", "derived": 1_000_000.0, "components": {"futures": 1_000_000.0}}]

    # the owner says the report leaves the FX legs out (a provisional choice of the policy): the break is explained
    pol = O.OcpPolicy(report_components=frozenset(set(O.COMPONENTS) - {"fx_legs"}))
    d1 = {(x["book_id"], x["ccy"]): x for x in run(report(-470_000), pol).comparisons}[("D1", "USD")]
    assert d1["break"] == pytest.approx(0) and not d1["over"] and d1["hints"] == []
    assert d1["not_in_report"] == pytest.approx({"fx_legs": 520_000})
    assert d1["reported"] == pytest.approx(d1["derived"] - sum(d1["not_in_report"].values()))


def test_sign_hint_and_entity_level():
    d1 = {(x["book_id"], x["ccy"]): x for x in run(report(-50_000)).comparisons}[("D1", "USD")]
    assert d1["over"] and any("знак" in h for h in d1["hints"])
    # an entity-level report (rows without book_id): the entity's books summed, compared per currency
    units = [{"scope": {"entity": "ENT1"}, "set_id": 7, "valid_date": ON, "row_count": 1}]
    rows = [{"entity": "ENT1", "ccy": "USD", "ocp_ccy": "670500", "_set_id": 7}]
    r = run(rows, units=units)
    assert [(x["level"], x["book_id"], x["ccy"]) for x in r.comparisons] == [("entity", None, "USD")]
    e = r.comparisons[0]  # D1, T1 and X1: the transfer nets out inside the entity
    assert e["break"] == pytest.approx(0) and e["components"]["bonds"] == 1_000_500
    assert e["components"].get("transfers", 0) == 0
    assert [x["book_id"] for x in r.outside_report] == ["F1"]


def test_transfers():
    t = {(x["book_id"], x["ccy"]): x for x in run(report(0)).transfers}
    d1 = t[("D1", "USD")]
    assert d1["ocp_to"] == "X1" and d1["before"] == pytest.approx(670_000) and d1["transferred"] == -620_000
    assert d1["after"] == pytest.approx(50_000) and d1["expected"] == pytest.approx(-670_000) and d1["over"]
    assert (d1["rate"], d1["value_date"]) == (81.25, "2026-10-16")
    assert d1["deals"][0]["trade_ids"] == ["XF-1"] and d1["deals"][0]["counterparty"] == "X1"
    x1 = t[("X1", "USD")]  # the receiving book: its mirror deal, no ocp_to of its own
    assert x1["ocp_to"] == "D1" and x1["transferred"] == 620_000 and not x1["over"]
    none = O.OcpPolicy(transfer_rule=O.TransferRule.NONE)
    r = run(report(0), none)
    assert r.per_position["D1|XFER"]["component"] == "cash" and not any(x["deals"] for x in r.transfers)


def test_cash_by_ledger_counts_a_balance_once_and_leaves_a_break_out():
    # the same balance reported by two bookkeeping systems (desk.cash): agreeing, it counts once; disagreeing, it has
    # no known value and is left out with a note (never the larger, the smaller or the first)
    two = [{"entity": "ENT1", "book_id": "D1", "ccy": "USD", "account_id": "MAIN", "balance_ccy": "100", "ledger": L}
           for L in ("L1", "L2")]
    lines, notes = O.cash_lines(two)
    assert [(x.book_id, x.ccy, x.amount) for x in lines] == [("D1", "USD", 100.0)] and notes == []
    split = [{**two[0], "account_id": "A"}, {**two[0], "account_id": "B", "balance_ccy": "50"}, two[1]]
    lines, notes = O.cash_lines(split)  # L1 over two accounts = 150, L2 = 100: a break
    assert lines == [] and len(notes) == 1 and "D1 USD" in notes[0] and "L1, L2" in notes[0]
    r = O.compute_ocp([], BOOKS, ON, cash=split)
    assert not r.lines and r.notes == notes


def test_entity_unit_with_and_without_books():
    # one entity unit: a row per book for D1 and an entity-level row for the entity's other books (T1, X1)
    units = [{"scope": {"entity": "ENT1"}, "set_id": 9, "valid_date": ON, "row_count": 2}]
    rows = [{"entity": "ENT1", "book_id": "D1", "ccy": "USD", "ocp_ccy": "50000", "_set_id": 9},
            {"entity": "ENT1", "ccy": "USD", "ocp_ccy": "620500", "_set_id": 9}]
    c = {(x["level"], x["book_id"]): x for x in run(rows, units=units).comparisons}
    assert set(c) == {("book", "D1"), ("entity", None)}
    assert c[("book", "D1")]["break"] == pytest.approx(0)
    assert c[("entity", None)]["break"] == pytest.approx(0) and c[("entity", None)]["in_report_file"]


def test_nominal_basis_skips_missing_amounts():
    rows = [core_row("D1|FWD", "D1", {"USD": 490_000.0}, "fx_forward", {"USD": None, "CNY": float("nan"), "EUR": 5.0})]
    r = O.compute_ocp(rows, BOOKS, ON, policy=O.OcpPolicy(amount_basis=O.AmountBasis.NOMINAL))
    assert [(x.ccy, x.amount) for x in r.lines] == [("EUR", 5.0)]


def test_compat_transfers_in_tdm_shape():
    t = run(report(0)).transfers
    assert ocp_transfers({"transfers": t}) == [{"from": "D1", "to": "X1", "ccy": "USD", "amount": 620_000.0,
                                                "before": pytest.approx(670_000), "rate": 81.25,
                                                "valueDate": "2026-10-16"}]
    assert ocp_transfers(None) == [] and ocp_transfers({"transfers": [{**t[0], "ocp_to": None}]}) == []


def test_provisional_conventions():
    nominal = run(report(0), O.OcpPolicy(amount_basis=O.AmountBasis.NOMINAL, bond_funding_liability=True))
    assert {x.position_id: x.amount for x in nominal.lines if x.component == "fx_legs"}["D1|FWD"] == 500_000
    no_ndf = run(report(0), O.OcpPolicy(include_ndf=False))
    assert "D1|NDF" not in no_ndf.per_position and any("NDF" in n for n in no_ndf.notes)
    no_cash = run(report(0), O.OcpPolicy(cash_from_balances=False))
    assert not any(x.key.startswith(("CASH|", "FUND|")) for x in no_cash.lines)
    window = run(report(0), O.OcpPolicy(settle_window_bd=0))
    assert window.per_position["D1|SPOT"]["component"] == "fx_legs"
    assert O.provisional(O.PROVISIONAL_POLICY) == [d.field for d in O.OPEN_DECISIONS]
    assert O.PROVISIONAL_POLICY.lineage()["report_components"] == list(O.COMPONENTS)
    with pytest.raises(ValueError):
        O.OcpPolicy(report_components=frozenset({"nope"}))


# ---- the published OCP (TDM#416) -----------------------------------------------------------------------------------

IN_REPORT = frozenset({"cash", "futures", "options", "transfers"})
COMPOSITION = O.OcpPolicy(report_components=IN_REPORT, include_ndf=False, bond_funding_liability=True)
"""An invented composition for the tests: the report holds the cash, the futures, the options and the transfers, NDFs
are left out of the OCP; the FX legs, bonds and funding are added from positions."""
OPTION = fx_row("D1|OPT", "D1", {"USD": 70_000.0}, "2026-12-17", x_product_group="fx_option")
OPTION["extra"]["reported"]["x_product_group"] = "fx_option"
F_UNITS = [*UNITS, {"scope": {"entity": "ENT2", "book_id": "F1"}, "set_id": 3, "valid_date": ON, "row_count": 0}]
D1_IN = 250_000 + 70_000 - 620_000
"""D1 in COMPOSITION's report: the cash 250k, the option 70k and the transfer -620k; outside it the FX legs 520k."""


def published_run(report_rows, policy=COMPOSITION, units=F_UNITS, rows=(*ROWS, OPTION), on=ON):
    return O.compute_ocp(list(rows), BOOKS, on, terms=TERMS, cash=CASH, liabilities=LIAB, report_rows=report_rows,
                         report_units=units, policy=policy)


def test_published_is_report_plus_outside_components_and_derived_outside_the_report():
    r = published_run(report(D1_IN))
    assert r.per_position["D1|OPT"]["component"] == "options" and "D1|NDF" not in r.per_position
    c = {(x["book_id"], x["ccy"]): x for x in r.comparisons}
    assert c[("D1", "USD")]["break"] == pytest.approx(0) and not c[("D1", "USD")]["over"]
    p = {(x["book_id"], x["ccy"]): x for x in r.published}
    d1 = p[("D1", "USD")]
    assert d1["source"] == "report" and d1["amount"] == pytest.approx(D1_IN + 520_000)
    assert d1["components"] == ["fx_legs"] and d1["outside"] == pytest.approx({"fx_legs": 520_000})
    assert d1["reported"] == D1_IN and d1["books"] == ["D1"]
    f1 = p[("F1", "CNY")]  # the futures book is covered and its report holds the futures but has no row: its 0
    assert f1["source"] == "report" and f1["amount"] == 0 and f1["components"] == []
    assert c[("F1", "CNY")]["over"] and c[("F1", "CNY")]["break"] == pytest.approx(-1_000_000)
    t1 = p[("T1", "USD")]  # bonds and funding are outside this report: its 0 + the bond net of its liability
    assert t1["source"] == "report" and t1["amount"] == pytest.approx(500) and t1["reported"] == 0
    assert t1["components"] == ["bonds", "funding"]
    x1 = p[("X1", "USD")]  # no report unit covers it: derived
    assert x1["source"] == "derived" and x1["amount"] == 620_000 and x1["reported"] is None
    by = O.published_by_currency(r.published)
    assert by["USD"]["total"] == pytest.approx(220_000 + 500 + 620_000)
    assert by["USD"]["by_source"] == pytest.approx({"report": 220_500, "derived": 620_000})
    g = {x["code"]: x for x in r.gaps}
    assert set(g) == {"outside_without_terms", "books_outside_report"}
    assert g["outside_without_terms"]["items"] == ["D1|NOTERMS"]  # a forward without terms outside: its nominal
    assert g["books_outside_report"]["items"] == ["X1"]
    # per position the FX delta is untouched: the derived lines are the same with any published basis
    derived = published_run(report(D1_IN), replace(COMPOSITION, published=O.PublishedBasis.DERIVED))
    assert [x.as_json() for x in derived.lines] == [x.as_json() for x in r.lines]
    assert {x["source"] for x in derived.published} == {"derived"} and derived.gaps == []
    assert O.published_by_currency(derived.published)["USD"]["total"] == pytest.approx(
        sum(v for v in O.by_currency(r.lines)["USD"].values()))


def test_published_with_cash_and_ndfs_outside_the_report():
    # another invented composition: the report holds the FX legs, the futures and the transfers; NDFs enter the OCP
    policy = O.OcpPolicy(report_components=frozenset({"fx_legs", "futures", "transfers"}), bond_funding_liability=True)
    r = published_run(report(520_000 - 620_000), policy)
    p = {(x["book_id"], x["ccy"]): x for x in r.published}
    d1 = p[("D1", "USD")]
    assert d1["components"] == ["cash", "ndf", "options"]
    assert d1["outside"] == pytest.approx({"cash": 250_000, "ndf": -100_000, "options": 70_000})
    assert d1["amount"] == pytest.approx(-100_000 + 250_000 - 100_000 + 70_000)
    assert p[("F1", "CNY")]["amount"] == 0 and p[("F1", "CNY")]["components"] == []
    assert {x["code"]: x for x in r.gaps}["outside_without_terms"]["items"] == ["D1|OPT", "D1|SPOT"]


def test_product_groups_classify_ndf_futures_and_options():
    rows = [fx_row("D1|N", "D1", {"USD": 1.0}, "2026-12-17", x_product_group="fx_ndf"),
            fx_row("D1|F", "D1", {"USD": 2.0}, "2026-12-17"), fx_row("D1|O", "D1", {"USD": 3.0})]
    rows[0]["extra"]["reported"]["x_product_group"] = "fx_ndf"
    rows[1]["extra"]["reported"]["x"] = {"x_product_group": "FX_FUTURE"}
    rows[2]["extra"]["reported"]["x_product_group"] = "fx_option_barrier"
    r = O.compute_ocp(rows, BOOKS, ON)
    assert {k: (v["component"], v["basis"]) for k, v in r.per_position.items()} == {
        "D1|N": ("ndf", "notional"), "D1|F": ("futures", "notional"), "D1|O": ("options", "amount")}
    assert "D1|N" not in O.compute_ocp(rows, BOOKS, ON, policy=O.OcpPolicy(include_ndf=False)).per_position


def test_stale_report_falls_back_to_derived_and_the_threshold_admits_it():
    old = [{**u, "valid_date": date(2026, 10, 13)} for u in F_UNITS]  # carried two days
    r = published_run(report(D1_IN), units=old)
    p = {(x["book_id"], x["ccy"]): x for x in r.published}
    assert p[("D1", "USD")]["source"] == "derived_stale_report"
    assert p[("D1", "USD")]["amount"] == pytest.approx(D1_IN + 520_000)  # all of D1 from positions
    assert p[("F1", "CNY")]["source"] == "derived_stale_report" and p[("F1", "CNY")]["amount"] == 1_000_000
    assert sum(1 for g in r.gaps if g["code"] == "stale_report") == 3
    ok = published_run(report(D1_IN), units=old, policy=replace(COMPOSITION, report_max_age_days=3))
    assert {(x["book_id"], x["source"]) for x in ok.published if x["book_id"] != "X1"} == {
        ("D1", "report"), ("F1", "report"), ("T1", "report")}
    nothing = published_run([], units=[])
    assert {x["source"] for x in nothing.published} == {"derived"}
    assert [g["code"] for g in nothing.gaps] == ["no_report", "outside_without_terms"]


def test_overlapping_entity_and_book_units_publish_each_book_once():
    # an entity unit of ENT1 (its total, D1 included) and a unit of D1 (TDM#214): D1 is published from its own unit,
    # the entity's figure without D1's report; the entity's total is the entity's report + the outside components.
    # ENT1's report: D1's -300k + X1's transfer 620k (T1's bonds and funding are outside it)
    units = [{"scope": {"entity": "ENT1"}, "set_id": 7, "valid_date": ON, "row_count": 1},
             {"scope": {"entity": "ENT1", "book_id": "D1"}, "set_id": 8, "valid_date": ON, "row_count": 1}]
    rows = [{"entity": "ENT1", "ccy": "USD", "ocp_ccy": "320000", "_set_id": 7},
            {"entity": "ENT1", "book_id": "D1", "ccy": "USD", "ocp_ccy": str(D1_IN), "_set_id": 8}]
    r = published_run(rows, units=units)
    assert len([c for c in r.comparisons if c["ccy"] == "USD"]) == 2  # the reconciliation is unchanged (TDM#214)
    p = {(x["book_id"], x["ccy"]): x for x in r.published if x["ccy"] == "USD"}
    assert set(p) == {("D1", "USD"), (None, "USD")}
    assert p[("D1", "USD")]["source"] == "report" and p[("D1", "USD")]["amount"] == pytest.approx(220_000)
    ent = p[(None, "USD")]
    assert ent["books"] == ["T1", "X1"] and ent["reported"] == pytest.approx(320_000 - D1_IN)
    assert ent["amount"] == pytest.approx(320_000 - D1_IN + 500)  # + T1's bond net of funding, outside this report
    total = sum(x["amount"] for x in p.values())
    assert total == pytest.approx(320_000 + 520_000 + 500)  # the entity's report + the outside parts, once
    assert any("итогом юрлица" in n and "D1" in n for n in r.notes)
    # the book's unit too old: the entity's report covers D1 again, nothing subtracted
    units[1]["valid_date"] = date(2026, 10, 1)
    p = {(x["book_id"], x["ccy"]): x for x in published_run(rows, units=units).published if x["ccy"] == "USD"}
    assert set(p) == {(None, "USD")} and p[(None, "USD")]["books"] == ["D1", "T1", "X1"]
    assert p[(None, "USD")]["amount"] == pytest.approx(320_000 + 520_000 + 500)


# ---- the adapter's ocp_policy (private/1.5, TDM#180) ---------------------------------------------------------------

def test_ocp_policy_table_sets_the_report_components_and_the_break_follows():
    from desk.rules.reference import apply_ocp_policy

    rows = [{"policy_id": "firm", "report_components": "Cash, FUTURES ,options,transfers",
             "include_ndf": False, "tolerance_ccy": "500", "report_max_age_days": 2, "published": "report_plus_outside"},
            {"policy_id": "old", "active": False, "report_components": "cash"}]
    s = apply_ocp_policy(rows, GROSS)
    assert s.policy.report_components == COMPOSITION.report_components and s.policy.tolerance_ccy == 500.0
    assert s.policy.report_max_age_days == 2 and s.policy.bond_funding_liability and not s.policy.include_ndf
    assert s.conflicts == ()
    assert s.source == {f: "ocp_policy:firm" for f in ("include_ndf", "published", "report_components",
                                                       "report_max_age_days", "tolerance_ccy")}
    assert O.provisional(s.policy, s.source) == ["amount_basis", "cash_from_balances", "settle_window_bd",
                                                 "transfer_rule", "transfer_target"]
    # the default (every component in the report, NDFs in): the report without the FX legs is a break;
    # with the table's composition it is explained
    d1 = {x["book_id"]: x for x in published_run(report(D1_IN), GROSS).comparisons}["D1"]
    assert d1["over"] and d1["break"] == pytest.approx(D1_IN - 120_000) and d1["not_in_report"] == {}
    pub = {x["book_id"]: x for x in published_run(report(D1_IN), GROSS).published}["D1"]
    assert pub["amount"] == D1_IN and pub["components"] == []  # the report as it is: it holds everything
    r = published_run(report(D1_IN), s.policy)
    d1 = {x["book_id"]: x for x in r.comparisons}["D1"]
    assert not d1["over"] and d1["not_in_report"] == pytest.approx({"fx_legs": 520_000})
    assert {x["book_id"]: x for x in r.published}["D1"]["amount"] == pytest.approx(220_000)


def test_ocp_policy_conflicts_keep_the_default():
    from desk.rules.reference import apply_ocp_policy

    two = [{"policy_id": "a", "report_components": "futures", "tolerance_ccy": "1000"},
           {"policy_id": "b", "report_components": "futures,ndf", "tolerance_ccy": "1000.0"}]
    s = apply_ocp_policy(two)
    assert s.policy.report_components == O.PROVISIONAL_POLICY.report_components  # ambiguous: the default
    assert s.source == {"tolerance_ccy": "ocp_policy:a,b"}  # the same tolerance twice is no conflict
    assert len(s.conflicts) == 1 and "report_components" in s.conflicts[0] and "a" in s.conflicts[0]
    bad = apply_ocp_policy([{"policy_id": "x", "report_components": "futures,swaps", "settle_window_bd": "1"}])
    assert bad.policy.report_components == O.PROVISIONAL_POLICY.report_components and bad.policy.settle_window_bd == 1
    assert "swaps" in bad.conflicts[0] and bad.source == {"settle_window_bd": "ocp_policy:x"}


def test_ocp_policy_rows_validate_and_components_match_the_validator():
    from desk.inbox.spi import OCP_COMPONENTS
    from desk.inbox.validate import check_reference_tables

    assert OCP_COMPONENTS == O.COMPONENTS
    books = [{"_src": "L2", "book_id": "D1", "name": "D1", "desk": "Rates", "book_type": "DERIVS", "entity": "ENT1",
              "active": True}]
    rows = [{"_src": "L2", "policy_id": "a", "report_components": "cash,options", "settle_window_bd": "2",
             "include_ndf": False, "tolerance_ccy": "100"},
            {"_src": "L3", "policy_id": "b", "report_components": "cash,opts", "tolerance_ccy": 100.0},
            {"_src": "L4", "policy_id": "c", "published": "everything"}]
    tables, issues = check_reference_tables({"books": books, "ocp_policy": rows})
    assert [r["policy_id"] for r in tables["ocp_policy"]] == ["a", "b"]
    assert tables["ocp_policy"][0]["settle_window_bd"] == 2 and tables["ocp_policy"][0]["active"] is True
    codes = sorted((i["severity"], i["code"]) for i in issues)
    assert codes == [("error", "bad_value"), ("warning", "ambiguous_policy"), ("warning", "unknown_component")]


@pytest.mark.parametrize("basis, fwd", [("spot_equivalent", 490_000), ("nominal", 500_000)])
def test_ocp_policy_rows_as_the_adapter_emits_them_with_the_amount_basis(basis, fwd):
    """The private adapter's rows: policy_id, report_components, published, include_ndf, report_max_age_days, the
    optional cash_from_balances, settle_window_bd, tolerance_ccy, amount_basis, and ``_src``."""
    from desk.inbox.validate import check_reference_tables
    from desk.rules.reference import apply_ocp_policy

    books = [{"_src": "L2", "book_id": "D1", "name": "D1", "desk": "Rates", "book_type": "DERIVS", "entity": "ENT1",
              "active": True}]
    row = {"_src": "L2", "policy_id": "firm", "report_components": "futures,bonds,other",
           "published": "report_plus_outside", "include_ndf": False, "report_max_age_days": 1,
           "cash_from_balances": False, "settle_window_bd": 1, "tolerance_ccy": "250", "amount_basis": basis}
    tables, issues = check_reference_tables({"books": books, "ocp_policy": [row]})
    assert issues == [] and tables["ocp_policy"][0]["_src"] == "L2"
    s = apply_ocp_policy(tables["ocp_policy"])
    assert s.conflicts == () and s.policy.amount_basis is O.AmountBasis(basis)
    assert s.source["amount_basis"] == "ocp_policy:firm" and "_src" not in s.source
    assert O.provisional(s.policy, s.source) == ["transfer_rule", "transfer_target"]
    # the basis: an FX derivative valued from its terms enters the derived OCP at core's FX delta or its nominal
    r = published_run(report(0), s.policy)
    assert {x.position_id: x.amount for x in r.lines if x.component == "fx_legs"}["D1|FWD"] == fwd
    bad = check_reference_tables({"books": books, "ocp_policy": [{**row, "amount_basis": "pv"}]})
    assert bad[0]["ocp_policy"] == [] and [i["code"] for i in bad[1]] == ["bad_value"]


def test_negative_windows_keep_the_default_and_warn():
    from desk.inbox.validate import check_reference_tables
    from desk.rules.reference import apply_ocp_policy

    s = apply_ocp_policy([{"policy_id": "x", "report_max_age_days": -1, "settle_window_bd": "-2",
                           "tolerance_ccy": "10"}])
    assert s.policy.report_max_age_days == O.PROVISIONAL_POLICY.report_max_age_days
    assert s.policy.settle_window_bd == O.PROVISIONAL_POLICY.settle_window_bd
    assert s.source == {"tolerance_ccy": "ocp_policy:x"} and len(s.conflicts) == 2
    books = [{"_src": "L2", "book_id": "D1", "name": "D1", "desk": "Rates", "book_type": "DERIVS", "entity": "ENT1",
              "active": True}]
    _, issues = check_reference_tables({"books": books, "ocp_policy": [
        {"_src": "L2", "policy_id": "x", "report_max_age_days": -1}]})
    assert [i["code"] for i in issues] == ["bad_policy_value"]


def test_an_outside_component_that_closes_the_break_is_a_double_count_gap():
    # the report holds the FX legs already (D1_IN + 520k): with the FX legs outside the composition they are added again
    r = published_run(report(D1_IN + 520_000))
    g = {x["code"]: x for x in r.gaps}
    assert "outside_double_count" in g and g["outside_double_count"]["items"] == ["книга D1 USD: PV валютных ног"]
    assert "outside_double_count" not in {x["code"] for x in published_run(report(D1_IN)).gaps}
