"""Funding rules and the market default rate in the monitor's settings (#237, ``desk.funding_settings``): the order
override -> adapter -> settings -> default (``rules.funding.RuleTier``), the HTM lot and REPO deal's own rate, the
settings.json shape and the settings API. Synthetic rules and rates only."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest
from fastapi.testclient import TestClient
from test_rules_funding import FX, MON, ONE_DAY, POLICY, lot, only, pos, run

from desk import funding_settings as FS
from desk.rules.funding import (
    DEFAULT_RATE_LABEL,
    CashBalance,
    FundingBase,
    FundingPolicy,
    FundingRule,
    FundingRules,
    HtmBase,
    LineKind,
    RepoDeal,
    RepoPolicy,
    RepoRate,
    RuleTier,
    compute_funding,
)
from desk.rules.rate_index import FixingTable
from desk.rules.reference import funding_rules_from_reference
from desk.settings import SettingsError, update_settings_json

RATES_D = FixingTable({"IDX_K": {date(2026, 9, 1): 0.10, MON: 0.12}, "IDX_ON": {MON: 0.095},
                       "RUONIA": {date(2026, 10, 15): 0.155}, "RUSFARCNY": {date(2026, 10, 15): 0.02}})
"""The engine tests' indices with a ruble and a yuan overnight index (synthetic values) for the default rate."""

ADAPTER = (FundingRule(ccy="RUB", book_type="TRADING", index="IDX_K", spread_bp=100, note="a_trading"),)
KW = {"ccy": "RUB", "book_type": "TRADING", "entity": "E1", "book_id": "B1"}


def _settings(**doc) -> FS.FundingSettings:
    fs = FS.from_doc(doc)
    assert fs.error is None, fs.error
    return fs


def run_d(**kw):
    return compute_funding(ONE_DAY, kw.pop("rules"), RATES_D, FX, policy=kw.pop("policy", POLICY), **kw)


def _all(fs: FS.FundingSettings, adapter=ADAPTER) -> FundingRules:
    return FundingRules((*adapter, *fs.rules().rates))


# ---------------------------------------------------------------------------------------------- the order
def test_precedence_override_adapter_settings_default():
    fs = _settings(rules=[
        {"rule_id": "s_book", "book_id": "B1", "rate_kind": "fixed", "fixed_rate_pct": 9},       # more specific
        {"rule_id": "s_usd", "ccy": "USD", "rate_kind": "fixed", "fixed_rate_pct": 5},
        {"rule_id": "s_ofz", "security_class": "ОФЗ-ПД", "override": True, "rate_kind": "index_spread",
         "index_code": "IDX_ON", "spread_bp": -25},
    ])
    rules = _all(fs)
    # the adapter's rule wins over a more specific settings rule
    assert rules.resolve(MON, **KW).note == "a_trading"
    # a settings rule where the adapter has none
    assert rules.resolve(MON, **{**KW, "ccy": "USD", "book_id": "B2"}).note == "s_usd"
    assert rules.resolve(MON, **{**KW, "ccy": "USD"}).note == "s_book"  # within the tier: the most specific
    # an override rule wins over the adapter's
    over = rules.resolve(MON, **KW, security_class="ОФЗ-ПД")
    assert (over.note, over.source, over.expr) == ("s_ofz", "settings_override", "IDX_ON - 25 bp")
    # nothing covers a CNY line but the default rate; nothing at all a EUR one
    cny = rules.resolve(MON, **{**KW, "ccy": "CNY", "book_type": "DERIVS", "book_id": "B2"})
    assert cny.tier == RuleTier.DEFAULT and cny.expr == f"RUSFARCNY ({DEFAULT_RATE_LABEL})"
    assert rules.resolve(MON, **{**KW, "ccy": "EUR", "book_type": "DERIVS", "book_id": "B2"}) is None
    # without the adapter's rule, the settings rule of the book, then the RUB default
    alone = _all(fs, adapter=())
    assert alone.resolve(MON, **KW).note == "s_book"
    assert alone.resolve(MON, **{**KW, "book_id": "B2"}).note == "default:RUB"


def test_default_rate_on_the_line_and_off():
    fs = _settings()
    p = pos()
    line = only(run_d(rules=_all(fs, adapter=()), positions=[p], previous_positions=[p]))
    assert line.default_rate and line.rule_source == "default" and line.rule_id == "default:RUB"
    assert line.rate_expr == f"RUONIA ({DEFAULT_RATE_LABEL})" and line.days[0].rate == pytest.approx(0.155)
    assert line.interest == pytest.approx(-1e6 * 0.155 / 365)
    cny = only(run_d(rules=fs.rules(), positions=[pos(ccy="CNY")], previous_positions=[pos(ccy="CNY")]))
    assert cny.rule_id == "default:CNY" and cny.days[0].rate == pytest.approx(0.02)
    usd = run_d(rules=fs.rules(), positions=[pos(ccy="USD")], previous_positions=[pos(ccy="USD")])
    assert not usd.lines and "no funding rule" in usd.problems[0].reason  # no dollar default: a problem, not RUONIA
    off = _settings(default_rate={"kind": "none"}, default_rate_by_ccy={})
    assert not off.default_enabled and off.default_rules() == ()
    fixed = _settings(default_rate={"kind": "fixed", "fixed_rate_pct": 16.5}, default_rate_by_ccy={
        "USD": {"kind": "index_spread", "index": "IDX_ON", "spread_bp": 50}})
    line = only(run(rules=fixed.rules(), positions=[p], previous_positions=[p]))
    assert line.days[0].rate == pytest.approx(0.165) and line.default_rate
    usd = only(run(rules=fixed.rules(), positions=[pos(ccy="USD")], previous_positions=[pos(ccy="USD")]))
    assert usd.days[0].rate == pytest.approx(0.096 + 0.005) and usd.rule_id == "default:USD"
    key = _settings(default_rate={"kind": "key_rate", "spread_bp": 100})
    assert key.default_rules()[0].expr == f"KEY_RATE + 100 bp ({DEFAULT_RATE_LABEL})"


def test_htm_lot_contract_rate_rules_and_default():
    adapter_htm = FundingRule(book_type="HTM", contract=True, note="a_htm")
    s_htm = {"rule_id": "s_htm", "book_type": "HTM", "rate_kind": "fixed", "fixed_rate_pct": 11}
    # the adapter's contract rule holds against an ordinary settings rule
    fs = _settings(rules=[s_htm])
    line = only(run(rules=_all(fs, adapter=(adapter_htm,)), htm_lots=[lot()]))
    assert line.rate_expr == "8.00% contract" and (line.rule_id, line.rule_source) == ("a_htm", "adapter")
    # without the adapter's rule the settings rule prices the lot; marked override, also against the adapter's
    assert only(run(rules=fs.rules(), htm_lots=[lot()])).days[0].rate == pytest.approx(0.11)
    over = _settings(rules=[{**s_htm, "override": True}])
    assert only(run(rules=_all(over, adapter=(adapter_htm,)), htm_lots=[lot()])).rule_source == "settings_override"
    # no rule: the lot's own rate; no rate either: the default rate; the adapter saying "contract" and no rate: a problem
    plain = _settings()
    assert only(run(rules=plain.rules(), htm_lots=[lot()])).rule_source == "contract"
    assert only(run_d(rules=plain.rules(), htm_lots=[lot(contract_rate=None)])).default_rate
    r = run(rules=FundingRules((adapter_htm,)), htm_lots=[lot(contract_rate=None)])
    assert not r.lines and r.problems[0].reason == "no contract rate"
    # a ruble rule of the adapter that names no book type does not reprice the lot
    general = FundingRule(ccy="RUB", fixed_rate=0.3)
    assert only(run(rules=FundingRules((general,)), htm_lots=[lot()])).rate_expr == "8.00% contract"


def test_contract_rule_is_no_rate_for_a_cash_line():
    """The adapter's HTM rule {book_type HTM, basis cost, rate_kind contract} (as in its example config) prices the HTM
    lots only: the cash of an HTM book (here of an entity funded on its whole cash) takes the general rule or the
    default rate, never "no contract" (review of #247)."""
    adapter_htm = FundingRule(book_type="HTM", contract=True, note="a_htm")
    cash = [CashBalance("H", "HTM", "OTH1", "RUB", 1e6)]
    entities = {"E1": "main", "OTH1": "other"}
    pol = replace(POLICY, entity_cash_rate_book_type=None)
    by_default = run_d(rules=_all(_settings(), adapter=(adapter_htm,)), cash=cash, previous_cash=cash,
                       entities=entities, policy=pol)
    line = only(by_default)
    assert not by_default.problems and line.kind is LineKind.ENTITY_CASH and line.default_rate
    assert line.days[0].rate == pytest.approx(0.155)
    general = FundingRule(ccy="RUB", fixed_rate=0.07, note="a_rub")
    by_rule = run_d(rules=FundingRules((adapter_htm, general)), cash=cash, previous_cash=cash, entities=entities,
                    policy=pol)
    assert not by_rule.problems and only(by_rule).rule_id == "a_rub"
    # the lots keep their contract rate under the same rules
    lots = only(run_d(rules=FundingRules((adapter_htm, general)), htm_lots=[lot()]))
    assert lots.rate_expr == "8.00% contract" and lots.rule_id == "a_htm"
    assert FundingRules((adapter_htm,)).resolve(MON, **{**KW, "book_type": "HTM"}) is None
    assert FundingRules((adapter_htm,)).resolve(MON, **{**KW, "book_type": "HTM"}, contracts=True).note == "a_htm"


def test_repo_rule_in_the_settings():
    deal = RepoDeal("R1", "RP", "DERIVS", "E1", "RUB", 1e6, MON, None, contract_rate=0.2)
    pol = replace(POLICY, repo=RepoPolicy(RepoRate.CONTRACT))
    assert only(run(rules=FundingRules(()), policy=pol, repos=[deal])).rate_expr == "20.00% REPO contract"
    fs = _settings(rules=[{"rule_id": "s_repo", "kind": "REPO", "rate_kind": "fixed", "fixed_rate_pct": 7}])
    line = only(run(rules=fs.rules(), policy=pol, repos=[deal]))
    assert line.days[0].rate == pytest.approx(0.07) and line.rule_id == "s_repo"


def test_cash_and_entity_rules_by_kind():
    cash = [CashBalance("D1", "DERIVS", "E1", "RUB", 1e6)]
    fs = _settings(rules=[{"rule_id": "s_cash", "kind": "CASH", "ccy": "RUB", "rate_kind": "fixed",
                           "fixed_rate_pct": 6}])
    line = only(run(rules=fs.rules(), cash=cash, previous_cash=cash))
    assert line.kind is LineKind.CASH and line.rule_id == "s_cash" and line.days[0].rate == pytest.approx(0.06)


def test_bases_policy_and_dec01():
    fs = _settings(rules=[{"rule_id": "s", "book_type": "BANKING", "basis": "cost", "rate_kind": "none"},
                          {"rule_id": "o", "book_type": "DERIVS", "basis": "none", "rate_kind": "none",
                           "override": True},
                          {"rule_id": "n", "book_type": "HTM", "basis": "mv_dirty", "rate_kind": "none"}],
                   policy={"htm_base": "clean_cost"})
    pol, source = fs.apply({"HTM": FundingBase.COST}, FundingPolicy())
    assert pol.base_by_book_type["BANKING"] is FundingBase.COST and source["base_by_book_type[BANKING]"] == "settings"
    assert pol.base_by_book_type["DERIVS"] is FundingBase.NONE  # override
    assert pol.base_by_book_type["HTM"] is FundingBase.COST  # the adapter's base holds
    assert pol.base_by_book_type["TRADING"] is FundingBase.MV_CLEAN and source["base_by_book_type[TRADING]"] == "DEC01"
    assert pol.htm_base is HtmBase.CLEAN_COST and source["htm_base"] == "settings"
    # rules of rate_kind none give no rate
    assert all(r.note not in ("s", "o", "n") for r in fs.rules().rates)


# ---------------------------------------------------------------------------------------------- shape
@pytest.mark.parametrize("doc, needle", [
    ([], "must be an object"),
    ({"x": 1}, "not a funding setting"),
    ({"default_rate": {"kind": "libor"}}, "default_rate.kind"),
    ({"default_rate": {"kind": "fixed"}}, "fixed_rate_pct"),
    ({"default_rate": {"kind": "index_spread"}}, "index"),
    ({"default_rate_by_ccy": {"RUB": {"kind": "ruonia"}}}, "default_rate"),
    ({"default_rate_by_ccy": {"usd": {"kind": "fixed", "fixed_rate_pct": 4}}}, "currency code"),
    ({"rules": [{"rate_kind": "fixed", "fixed_rate_pct": 1}]}, "rule_id"),
    ({"rules": [{"rule_id": "r", "rate_kind": "fixed", "fixed_rate_pct": 1, "spread_bp": 5}]}, "spread"),
    ({"rules": [{"rule_id": "r", "rate_kind": "index_spread"}]}, "index_code"),
    ({"rules": [{"rule_id": "r", "kind": "SWAP", "rate_kind": "contract"}]}, "kind"),
    ({"rules": [{"rule_id": "r", "basis": "cost", "rate_kind": "none"}]}, "book_type"),
    ({"rules": [{"rule_id": "r", "rate_kind": "contract", "colour": 1}]}, "not a rule field"),
    ({"rules": [{"rule_id": "r", "rate_kind": "contract"}, {"rule_id": "r", "rate_kind": "contract"}]}, "repeat"),
    ({"rules": [{"rule_id": "a", "book_type": "T", "basis": "cost", "rate_kind": "none"},
                {"rule_id": "b", "book_type": "T", "basis": "cash", "rate_kind": "none"}]}, "two bases"),
    ({"policy": {"htm_base": "dirty"}}, "htm_base"),
    ({"policy": {"colour": "red"}}, "not a policy field"),
])
def test_check(doc, needle):
    errors = FS.check(doc)
    assert errors and any(needle in e for e in errors), errors
    assert FS.from_doc(doc).error  # a malformed object: the defaults, and the error said


def test_settings_json_round_trip(tmp_path):
    doc = {"default_rate": {"kind": "key_rate", "spread_bp": -50},
           "rules": [{"rule_id": "r", "entity": "E1", "rate_kind": "fixed", "fixed_rate_pct": 7.5}]}
    update_settings_json({"funding": doc}, tmp_path)
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["funding"] == doc
    with pytest.raises(SettingsError, match="default_rate.kind"):
        update_settings_json({"funding": {"default_rate": {"kind": "x"}}}, tmp_path)
    rows = FS.from_doc(doc).rule_rows
    assert rows[0]["source"] == "settings" and rows[0]["active"] is True
    assert funding_rules_from_reference(rows, RuleTier.SETTINGS).rates[0].tier == RuleTier.SETTINGS


def test_settings_api(tmp_path, monkeypatch):
    from desk.api import create_app

    monkeypatch.setenv("DESK_HOME", str(tmp_path))
    c = TestClient(create_app("postgresql://unused"), base_url="http://127.0.0.1:8000",
                   client=("127.0.0.1", 50000))
    d = c.get("/api/v1/settings/funding").json()
    assert d["stored"] is None and d["effective"]["default_rate"] == FS.DEFAULTS["default_rate"]
    assert d["default"]["by_ccy"]["RUB"]["expr"].startswith("RUONIA") and d["options"]["hint"] == FS.HINT
    body = {"default_rate": {"kind": "fixed", "fixed_rate_pct": 15},
            "rules": [{"rule_id": "x", "book_type": "TRADING", "rate_kind": "index_spread", "index_code": "RUONIA",
                       "spread_bp": 75, "override": True}]}
    r = c.put("/api/v1/settings/funding", json=body)
    assert r.status_code == 200 and r.json()["stored"] == body
    assert r.json()["default"]["by_ccy"]["RUB"]["expr"] == f"15.00% fixed ({DEFAULT_RATE_LABEL})"
    bad = c.put("/api/v1/settings/funding", json={"default_rate": {"kind": "fixed"}})
    assert bad.status_code == 422 and "fixed_rate_pct" in bad.text
    assert c.get("/api/v1/settings").json()["funding"] == body  # the same settings.json
    assert c.request("DELETE", "/api/v1/settings/funding", json={}).json()["stored"] is None  # back to the defaults
