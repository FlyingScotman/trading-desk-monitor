"""P1's reference tables (private/1.2 ``entities``, ``funding_rules``) as P2's funding engine inputs
(``desk.rules.reference``), on the synthetic fixture config of the reference adapter."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from desk.inbox import reference as reference_adapter
from desk.inbox.validate import check_reference_tables
from desk.rules.funding import DayBasis, EntityType, FundingBase, Periodicity
from desk.rules.reference import bases_from_reference, entities_from_reference, funding_rules_from_reference

DS_CONFIG = Path(__file__).parent / "fixtures" / "private" / "v1" / "datasets" / "config"
D = date.fromisoformat


@pytest.fixture(scope="module")
def tables() -> dict:
    tables, issues = check_reference_tables(reference_adapter.reference(SimpleNamespace(config_dir=str(DS_CONFIG))))
    assert [i for i in issues if i["severity"] == "error"] == []
    return tables


def test_funding_rules_from_the_fixture(tables: dict) -> None:
    rules = funding_rules_from_reference(tables["funding_rules"])
    # contract (HTM lots) and none (futures) are not rates
    assert sorted(r.note for r in rules.rates) == ["derivs_rub", "ent2_cash", "trading_rub", "trading_rub",
                                                   "trading_usd"]
    old, new = sorted((r for r in rules.rates if r.note == "trading_rub"), key=lambda r: r.valid_from)
    assert (old.valid_from, old.valid_to, new.valid_from, new.valid_to) == (date.min, D("2026-10-19"),
                                                                            D("2026-10-19"), None)
    kw = {"ccy": "RUB", "book_type": "TRADING", "entity": "ENT1", "book_id": "T1"}
    assert rules.resolve(D("2026-10-16"), **kw).expr == "KEY_RATE + 100 bp"
    assert rules.resolve(D("2026-10-20"), **kw).expr == "KEY_RATE + 150 bp"
    usd = rules.resolve(D("2026-10-20"), **{**kw, "ccy": "USD"})
    assert usd.fixed_rate == pytest.approx(0.05) and usd.day_basis is DayBasis.ACT_360
    # the entity's cash rule outranks the book type's (entity 4 + ccy 2 > book type 1 + ccy 2)
    assert rules.resolve(D("2026-10-20"), **{**kw, "entity": "ENT2", "book_id": "C1"}).note == "ent2_cash"
    assert rules.resolve(D("2026-10-20"), **{**kw, "book_type": "FUTURES", "book_id": "F1"}) is None
    assert rules.extra_spreads == ()


def test_bases_and_entities_from_the_fixture(tables: dict) -> None:
    assert bases_from_reference(tables["funding_rules"]) == {
        "TRADING": FundingBase.MV_CLEAN, "DERIVS": FundingBase.CASH, "FUTURES": FundingBase.NONE,
        "HTM": FundingBase.COST}
    types, sweeps = entities_from_reference(tables["entities"])
    assert types == {"ENT1": EntityType.MAIN, "ENT2": EntityType.OTHER}
    assert sweeps == {"ENT2": Periodicity.MONTHLY}


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
