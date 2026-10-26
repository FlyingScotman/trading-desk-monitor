"""The OCP engine (``desk.rules.ocp``, wave B2, TDM#59) on synthetic rows in the shape of the run's content: the
components, the reconciliation with a report at book and entity level with its explanation by component and hints,
the transfers, and the provisional conventions (OQ-11 nominal basis and NDFs, report components, DEC06)."""

from __future__ import annotations

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
                    "D1|NDF": "fx_legs", "F1|FUT": "futures", "D1|XFER": "transfers", "X1|XFER": "transfers"}
    assert "T1|OFZ" not in r.per_position  # a RUB row has no OCP
    d = O.derive(r.lines)
    assert d[("ENT1", "D1", "USD")] == pytest.approx({"cash": 250_000, "fx_legs": 420_000, "transfers": -620_000})
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
    r = run(report(-370_000))
    c = {(x["book_id"], x["ccy"]): x for x in r.comparisons}
    d1 = c[("D1", "USD")]
    assert d1["derived"] == pytest.approx(50_000) and d1["in_report"] == pytest.approx(50_000)
    assert d1["break"] == pytest.approx(-420_000) and d1["over"]
    assert any("PV валютных ног" in h and "не включает" in h for h in d1["hints"])
    t1 = c[("T1", "USD")]  # a covered book without rows: reported 0; bonds net of their liability are within tolerance
    assert t1["reported"] == 0 and not t1["in_report_file"] and t1["break"] == pytest.approx(-500) and not t1["over"]
    assert r.outside_report == [  # books no report unit covers: listed, not breaks
        {"entity": "ENT1", "book_id": "X1", "ccy": "USD", "derived": 620_000.0, "components": {"transfers": 620_000.0}},
        {"entity": "ENT2", "book_id": "F1", "ccy": "CNY", "derived": 1_000_000.0, "components": {"futures": 1_000_000.0}}]

    # the owner says the report leaves the FX legs out (a provisional choice of the policy): the break is explained
    pol = O.OcpPolicy(report_components=frozenset(set(O.COMPONENTS) - {"fx_legs"}))
    d1 = {(x["book_id"], x["ccy"]): x for x in run(report(-370_000), pol).comparisons}[("D1", "USD")]
    assert d1["break"] == pytest.approx(0) and not d1["over"] and d1["hints"] == []
    assert d1["not_in_report"] == pytest.approx({"fx_legs": 420_000})
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
