"""P1's reference tables (private/1.2 ``entities``, ``funding_rules``) as P2's funding engine inputs
(``desk.rules.reference``), on the synthetic fixture config of the reference adapter."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from desk.inbox import reference as reference_adapter
from desk.inbox.validate import check_reference_tables
from desk.rules.funding import DayBasis, EntityType, FundingBase, FundingPolicy, Periodicity, WriteOffPolicy
from desk.rules.reference import (
    bases_from_reference,
    entities_from_reference,
    funding_rules_from_reference,
    policy_from_reference,
)

DS_CONFIG = Path(__file__).parent / "fixtures" / "private" / "v1" / "datasets" / "config"
D = date.fromisoformat


@pytest.fixture(scope="module")
def tables() -> dict:
    tables, issues = check_reference_tables(reference_adapter.reference(SimpleNamespace(config_dir=str(DS_CONFIG))))
    assert [i for i in issues if i["severity"] == "error"] == []
    return tables


def test_funding_rules_from_the_fixture(tables: dict) -> None:
    rules = funding_rules_from_reference(tables["funding_rules"])
    # none (futures) is not a rate; contract (HTM lots: the lot's own rate) is a contract rule (#237)
    assert sorted(r.note for r in rules.rates) == ["derivs_rub", "derivs_usd", "ent2_cash", "ent2_cash_usd", "htm",
                                                   "trading_rub", "trading_rub", "trading_usd"]
    assert [r.contract for r in rules.rates if r.note == "htm"] == [True]
    old, new = sorted((r for r in rules.rates if r.note == "trading_rub"), key=lambda r: r.valid_from)
    assert (old.valid_from, old.valid_to, new.valid_from, new.valid_to) == (date.min, D("2026-10-19"),
                                                                            D("2026-10-19"), None)
    kw = {"ccy": "RUB", "book_type": "TRADING", "entity": "ENT1", "book_id": "T1"}
    assert rules.resolve(D("2026-10-16"), **kw).expr == "KEY_RATE + 100 bp"
    assert rules.resolve(D("2026-10-20"), **kw).expr == "KEY_RATE + 150 bp"
    usd = rules.resolve(D("2026-10-20"), **{**kw, "ccy": "USD"})
    assert usd.fixed_rate == pytest.approx(0.06) and usd.day_basis is DayBasis.ACT_360
    # the entity's cash rule outranks the book type's (entity 16 + ccy 4 > book type 1 + ccy 4)
    assert rules.resolve(D("2026-10-20"), **{**kw, "entity": "ENT2", "book_id": "C1"}).note == "ent2_cash"
    # B3 (#61): the history file's USD balances have their rates too (a cash book, the other entity)
    assert rules.resolve(D("2026-10-21"), **{**kw, "entity": "ENT2", "book_id": "C1", "ccy": "USD"}).note \
        == "ent2_cash_usd"
    assert rules.resolve(D("2026-10-21"), **{**kw, "book_type": "DERIVS", "book_id": "D1", "ccy": "USD"}).note \
        == "derivs_usd"
    assert rules.resolve(D("2026-10-20"), **{**kw, "book_type": "FUTURES", "book_id": "F1"}) is None
    assert rules.extra_spreads == ()


def test_bases_and_entities_from_the_fixture(tables: dict) -> None:
    assert bases_from_reference(tables["funding_rules"]) == {
        "TRADING": FundingBase.MV_CLEAN, "DERIVS": FundingBase.CASH, "FUTURES": FundingBase.NONE,
        "HTM": FundingBase.COST}
    types, sweeps = entities_from_reference(tables["entities"])
    assert types == {"ENT1": EntityType.MAIN, "ENT2": EntityType.OTHER}
    assert sweeps == {"ENT2": Periodicity.MONTHLY}


def test_policy_from_the_fixture(tables: dict) -> None:
    """The adapter's settings, not the engine's defaults, give the market-value base and the per-entity sweep."""
    assert "TRADING" not in FundingPolicy().base_by_book_type
    pol = policy_from_reference(tables["funding_rules"], tables["entities"])
    assert pol.base_by_book_type["TRADING"] is FundingBase.MV_CLEAN
    assert pol.write_off.periodicity("TRADING", "ENT2", EntityType.OTHER) is Periodicity.MONTHLY
    assert pol.write_off.periodicity("DERIVS", "ENT1") is None   # not in the contract: still unset
    keep = FundingPolicy(write_off=WriteOffPolicy(by_entity={"ENT9": Periodicity.DAILY}))
    assert policy_from_reference([], [], keep).write_off.by_entity == {"ENT9": Periodicity.DAILY}


def test_retired_revisions_and_conflicting_bases() -> None:
    base = {"_src": "L2", "rule_id": "r", "book_type": "TRADING", "basis": "mv_clean", "rate_kind": "fixed",
            "fixed_rate_pct": "6.5"}
    rows = [base, {**base, "valid_from": "2026-10-19", "active": False}]
    (only,) = funding_rules_from_reference(rows).rates
    assert only.fixed_rate == pytest.approx(0.065) and only.valid_to == D("2026-10-19")
    assert funding_rules_from_reference(rows).resolve(D("2026-10-20"), ccy="RUB", book_type="TRADING",
                                                      entity="E", book_id="B") is None
    with pytest.raises(ValueError, match="two bases"):
        bases_from_reference([base, {**base, "rule_id": "r2", "ccy": "USD", "basis": "mv_dirty"}])
    ent = [{"entity": "E", "funding_scheme": "cash_balance", "pnl_sweep": "daily"},
           {"entity": "E", "funding_scheme": "by_book_type", "valid_from": "2026-10-01", "active": False}]
    assert entities_from_reference(ent) == ({}, {})
