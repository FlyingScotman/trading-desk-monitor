"""Funding in the monitor's own settings (#237): the market default rate and funding rules for anything that is funded,
kept in ``<home>/settings.json`` under the key ``funding`` (``GET``/``PUT /api/v1/settings/funding``, the settings
page's «Фондирование»). Only the mechanism lives here: the organisation's rates and spreads are entered by the owner on
the work machine, never committed (ADR-008).

**Order** (``rules.funding.RuleTier``): a settings rule marked ``override`` -> the adapter's ``funding_rules`` -> a
settings rule -> the market default rate. A lower tier wins whatever the specificity; within a tier the most specific
rule. Each funding line says which rule gave its rate (``rule_id``, ``rule_source``).

**The market default rate** (``default_rate``, per currency ``default_rate_by_ccy``) funds a line no rule covers; the run
and the pages name it «рыночная ставка по умолчанию» (``rules.funding.DEFAULT_RATE_LABEL``), so it is not mistaken for a
transfer rate. Kinds (``DEFAULT_KINDS``): ``none`` (not funded; with no rule anywhere the run says «не настроено»),
``ruonia``, ``key_rate``, ``fixed`` (``fixed_rate_pct``) and ``index_spread`` (any hub index ``index`` +
``spread_bp``, a negative spread for a rate below the index); ``ruonia`` and ``key_rate`` take a ``spread_bp`` too.
``default_rate`` is the reporting currency's (RUB); another currency is funded at the default only when
``default_rate_by_ccy`` names it (a RUB rate is never applied to a dollar body).

**The default when nothing is set** (``DEFAULTS``): RUB at RUONIA flat, CNY at RUSFARCNY flat, other currencies none.
RUONIA is the ruble's market overnight rate (published by the CBR, in the hub, the base of the RUONIA OIS curve the run
discounts on) and is neither the key rate (a policy rate) nor a transfer rate; RUSFARCNY is the hub's overnight yuan
repo rate. The hub has no dollar or euro overnight fixing, so USD and EUR stay unset: such a line is a funding problem
that says so, until the owner sets a fixed rate or an index for it.

**Rules** (``rules``) have the shape of the adapter's ``funding_rules`` rows (08 §10: ``rule_id``, ``valid_from``,
``active``, the selectors ``book_type`` / ``entity`` / ``book_id`` / ``ccy`` / ``instrument_id``, ``basis``,
``rate_kind`` ``index_spread`` / ``fixed`` / ``contract`` / ``none``, ``index_code``, ``spread_bp``, ``fixed_rate_pct``,
``day_count``) plus ``security_class`` (the bond's class as the run shows it: ОФЗ-ПД, Корпоративные, ...), ``kind``
(``POS`` a bond by MV, ``HTM`` a purchase lot, ``CASH`` a book's cash, ``ENTITY_CASH`` an entity funded on its cash,
``REPO`` a deal), ``override`` and ``note``. A rule naming ``HTM`` or the lot's book type prices an HTM lot instead of
its contract rate; one naming ``REPO`` prices a REPO deal. ``basis`` gives the book type's funding base where the
adapter gives none (an ``override`` rule's over the adapter's).

**Policy** (``policy``): the firm-wide conventions of the adapter's ``funding_policy`` (``rules.reference.POLICY_FIELDS``:
``htm_base``, ``mv_base_date``, ...) where the adapter gives none.

The market-value base of a TRADING book that neither the adapter nor the settings name is clean MV: the owner's DEC01
(23.10.2026, FlyingScotman/trading-desk-monitor#53).
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from .rules.funding import (
    DEFAULT_RATE_LABEL,
    DayBasis,
    FundingBase,
    FundingPolicy,
    FundingRule,
    FundingRules,
    LineKind,
    RuleTier,
)
from .rules.reference import (
    POLICY_FIELDS,
    apply_funding_policy,
    bases_from_reference,
    funding_rules_from_reference,
)

KEY = "funding"
"""The settings.json key."""
DEFAULT_KINDS = ("none", "ruonia", "key_rate", "fixed", "index_spread")
KIND_INDEX = {"ruonia": "RUONIA", "key_rate": "KEY_RATE"}
KIND_TEXT = {"none": "не фондировать (не настроено)", "ruonia": "RUONIA", "key_rate": "ключевая ставка",
             "fixed": "фиксированная ставка", "index_spread": "ставка ± спред"}
RATE_KINDS = ("index_spread", "fixed", "contract", "none")
BASES = tuple(b.value for b in FundingBase)
LINE_KINDS = tuple(k.value for k in LineKind)
DAY_COUNTS = tuple(d.value for d in DayBasis)
SELECTORS = ("book_type", "entity", "book_id", "security_class", "kind", "ccy", "instrument_id")
RULE_KEYS = ("rule_id", "valid_from", "active", "override", *SELECTORS, "basis", "rate_kind", "index_code", "spread_bp",
             "fixed_rate_pct", "day_count", "note")
REPORTING_CCY = "RUB"
DEC01_TRADING_BASE = FundingBase.MV_CLEAN
"""DEC01 (owner, 23.10.2026): a market-value book is funded on its clean MV."""
DEFAULTS: dict[str, Any] = {
    "default_rate": {"kind": "ruonia", "spread_bp": 0},
    "default_rate_by_ccy": {"CNY": {"kind": "index_spread", "index": "RUSFARCNY", "spread_bp": 0}},
    "rules": [],
    "policy": {},
}
HINT = ("По умолчанию: рубль — RUONIA без спреда, юань — RUSFARCNY без спреда, остальные валюты не заданы. RUONIA — "
        "рыночная ставка овернайт (публикует ЦБ, есть в хабе), не ключевая и не трансфертная. Правила здесь действуют, "
        "где их не дал адаптер; правило с «главнее адаптера» — и там, где дал. Ставки организации вводятся только на "
        "рабочей машине.")


class FundingSettingsError(ValueError):
    pass


def _num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def _str(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _check_rate(path: str, r: Any, errors: list[str]) -> None:
    if not isinstance(r, dict):
        errors.append(f"{path}: must be an object {{kind, index, spread_bp, fixed_rate_pct}}")
        return
    kind = r.get("kind")
    if kind not in DEFAULT_KINDS:
        errors.append(f"{path}.kind: one of {', '.join(DEFAULT_KINDS)}")
        return
    if r.get("spread_bp") is not None and not _num(r["spread_bp"]):
        errors.append(f"{path}.spread_bp: must be a number")
    if r.get("day_count") is not None and r["day_count"] not in DAY_COUNTS:
        errors.append(f"{path}.day_count: one of {', '.join(DAY_COUNTS)}")
    if kind == "fixed":
        if not _num(r.get("fixed_rate_pct")):
            errors.append(f"{path}.fixed_rate_pct: a fixed rate needs a number, %")
        if r.get("spread_bp"):
            errors.append(f"{path}.spread_bp: a fixed rate takes no spread")
    if kind == "index_spread" and not _str(r.get("index")):
        errors.append(f"{path}.index: the index code (RUONIA, KEY_RATE, RUSFARCNY, ...)")


def _is_date(v: Any) -> bool:
    try:
        return isinstance(v, str) and len(v) == 10 and date.fromisoformat(v) is not None
    except ValueError:
        return False


def _check_rule(path: str, r: Any, errors: list[str]) -> None:
    if not isinstance(r, dict):
        errors.append(f"{path}: must be an object")
        return
    if not _str(r.get("rule_id")):
        errors.append(f"{path}.rule_id: required")
    for k in r:
        if k not in RULE_KEYS:
            errors.append(f"{path}.{k}: not a rule field ({', '.join(RULE_KEYS)})")
    for k in ("book_type", "entity", "book_id", "security_class", "ccy", "instrument_id", "index_code", "note"):
        if r.get(k) is not None and not isinstance(r[k], str):
            errors.append(f"{path}.{k}: must be a string")
    for k in ("active", "override"):
        if r.get(k) is not None and not isinstance(r[k], bool):
            errors.append(f"{path}.{k}: must be true or false")
    if r.get("valid_from") is not None and not _is_date(r["valid_from"]):
        errors.append(f"{path}.valid_from: a date YYYY-MM-DD")
    if r.get("kind") is not None and r["kind"] not in LINE_KINDS:
        errors.append(f"{path}.kind: one of {', '.join(LINE_KINDS)}")
    if r.get("basis") is not None and r["basis"] not in BASES:
        errors.append(f"{path}.basis: one of {', '.join(BASES)}")
    if r.get("basis") is not None and not r.get("book_type"):
        errors.append(f"{path}.basis: the base is a book type's, the rule needs book_type")
    if r.get("day_count") is not None and r["day_count"] not in DAY_COUNTS:
        errors.append(f"{path}.day_count: one of {', '.join(DAY_COUNTS)}")
    kind = r.get("rate_kind")
    if kind not in RATE_KINDS:
        errors.append(f"{path}.rate_kind: one of {', '.join(RATE_KINDS)}")
    elif kind == "index_spread" and not _str(r.get("index_code")):
        errors.append(f"{path}.index_code: an index rate needs the index code")
    elif kind == "fixed" and not _num(r.get("fixed_rate_pct")):
        errors.append(f"{path}.fixed_rate_pct: a fixed rate needs a number, %")
    elif kind == "none" and r.get("basis") is None:
        errors.append(f"{path}.basis: a rule without a rate (none) only gives a base")
    if r.get("spread_bp") is not None and not _num(r["spread_bp"]):
        errors.append(f"{path}.spread_bp: must be a number")
    elif r.get("spread_bp") and kind != "index_spread":
        errors.append(f"{path}.spread_bp: only an index rate takes a spread")
    if kind != "fixed" and r.get("fixed_rate_pct") is not None:
        errors.append(f"{path}.fixed_rate_pct: only a fixed rate")


def check(doc: Any) -> list[str]:
    """Shape errors of the settings.json ``funding`` object (None: not set, the defaults)."""
    if doc is None:
        return []
    if not isinstance(doc, dict):
        return [f"{KEY}: must be an object {{default_rate, default_rate_by_ccy, rules, policy}}"]
    errors: list[str] = []
    for k in doc:
        if k not in DEFAULTS:
            errors.append(f"{KEY}.{k}: not a funding setting ({', '.join(DEFAULTS)})")
    if doc.get("default_rate") is not None:
        _check_rate(f"{KEY}.default_rate", doc["default_rate"], errors)
    by_ccy = doc.get("default_rate_by_ccy")
    if by_ccy is not None:
        if not isinstance(by_ccy, dict):
            errors.append(f"{KEY}.default_rate_by_ccy: must be an object {{currency: rate}}")
        else:
            for c, r in by_ccy.items():
                if not (isinstance(c, str) and len(c) == 3 and c.isalpha() and c.isupper()):
                    errors.append(f"{KEY}.default_rate_by_ccy: {c!r} is not a currency code (USD)")
                elif c == REPORTING_CCY:
                    errors.append(f"{KEY}.default_rate_by_ccy.{c}: the ruble's rate is default_rate")
                else:
                    _check_rate(f"{KEY}.default_rate_by_ccy.{c}", r, errors)
    rules = doc.get("rules")
    if rules is not None:
        if not isinstance(rules, list):
            errors.append(f"{KEY}.rules: must be a list")
        else:
            seen: set[tuple] = set()
            for i, r in enumerate(rules):
                _check_rule(f"{KEY}.rules[{i}]", r, errors)
                if isinstance(r, dict):
                    k = (r.get("rule_id"), r.get("valid_from"))
                    if k in seen:
                        errors.append(f"{KEY}.rules[{i}]: rule_id and valid_from repeat an earlier rule")
                    seen.add(k)
    pol = doc.get("policy")
    if pol is not None:
        if not isinstance(pol, dict):
            errors.append(f"{KEY}.policy: must be an object")
        else:
            for k, v in pol.items():
                if k not in POLICY_FIELDS:
                    errors.append(f"{KEY}.policy.{k}: not a policy field ({', '.join(POLICY_FIELDS)})")
                    continue
                if v is None:
                    continue
                try:
                    t = POLICY_FIELDS[k][1]
                    if t is int and not (_num(v) and v == int(v) and v >= 0):
                        raise ValueError
                    t(v)
                except (TypeError, ValueError):
                    errors.append(f"{KEY}.policy.{k}: bad value")
    if not errors:
        try:
            _bases(doc.get("rules") or [])
        except ValueError as e:
            errors.append(f"{KEY}.rules: {e}")
    return errors


def _bases(rows: list[dict]) -> tuple[dict[str, FundingBase], dict[str, FundingBase]]:
    """The book type bases the settings' rules give: (ordinary, override)."""
    return (bases_from_reference([r for r in rows if not r.get("override")]),
            bases_from_reference([r for r in rows if r.get("override")]))


def _row(r: dict[str, Any]) -> dict[str, Any]:
    """A settings rule in the adapter's row form (as ``funding_rules_from_reference`` reads it)."""
    out = {k: v for k, v in r.items() if v is not None and v != ""}
    out.setdefault("active", True)
    out["source"] = RuleTier.SOURCE[RuleTier.OVERRIDE if r.get("override") else RuleTier.SETTINGS]
    return out


@dataclass(frozen=True)
class FundingSettings:
    """The effective funding settings (``load``): the stored object over ``DEFAULTS``."""

    default_rate: dict[str, Any]
    default_rate_by_ccy: dict[str, dict[str, Any]]
    rule_rows: tuple[dict[str, Any], ...] = ()
    policy: dict[str, Any] = field(default_factory=dict)
    stored: bool = False
    """settings.json has a ``funding`` object (else every value is a default)."""
    error: str | None = None
    """settings.json could not be read or its ``funding`` is malformed: the defaults are used, and the run says so."""

    # -- the default rate
    @property
    def default_enabled(self) -> bool:
        return self.default_rate.get("kind") != "none" or any(
            r.get("kind") != "none" for r in self.default_rate_by_ccy.values())

    def _default_rule(self, ccy: str, r: dict[str, Any]) -> FundingRule | None:
        kind = r.get("kind")
        if kind == "none":
            return None
        basis = DayBasis(r["day_count"]) if r.get("day_count") else None
        common = {"ccy": ccy, "tier": RuleTier.DEFAULT, "note": f"default:{ccy}", "day_basis": basis}
        if kind == "fixed":
            return FundingRule(fixed_rate=float(r["fixed_rate_pct"]) / 100, **common)
        index = KIND_INDEX.get(kind) or str(r.get("index") or "").strip()
        return FundingRule(index=index, spread_bp=float(r.get("spread_bp") or 0), **common)

    def default_rules(self) -> tuple[FundingRule, ...]:
        out = [self._default_rule(REPORTING_CCY, self.default_rate)]
        out += [self._default_rule(c, r) for c, r in sorted(self.default_rate_by_ccy.items())]
        return tuple(r for r in out if r is not None)

    def describe(self) -> dict[str, Any]:
        """The default in force, for the run's block and the pages."""
        rules = {r.ccy: r for r in self.default_rules()}
        rates = {REPORTING_CCY: self.default_rate, **self.default_rate_by_ccy}
        return {"label": DEFAULT_RATE_LABEL, "enabled": self.default_enabled, "stored": self.stored,
                "by_ccy": {c: {"kind": r.get("kind"), "kind_text": KIND_TEXT.get(r.get("kind"), r.get("kind")),
                               "expr": (rules[c].expr if c in rules else None)}
                           for c, r in sorted(rates.items())},
                "hint": HINT, **({"error": self.error} if self.error else {})}

    # -- the rules
    @property
    def active_rules(self) -> tuple[dict[str, Any], ...]:
        return tuple(r for r in self.rule_rows if r.get("active", True) is not False)

    def rules(self) -> FundingRules:
        """The settings' rules (tiers override and settings) and the default rate (tier default)."""
        own = funding_rules_from_reference(
            self.rule_rows, tier=lambda r: RuleTier.OVERRIDE if r.get("override") else RuleTier.SETTINGS)
        return FundingRules(rates=(*own.rates, *self.default_rules()))

    def apply(self, adapter_bases: dict[str, FundingBase], policy: FundingPolicy
              ) -> tuple[FundingPolicy, dict[str, str]]:
        """``policy`` (with the adapter's bases already in) with the settings' bases and conventions under the
        adapter's (an override rule's base over it) and DEC01's clean base of TRADING; the source of each field set."""
        normal, over = _bases(list(self.active_rules))
        bases = dict(policy.base_by_book_type)
        source: dict[str, str] = {}
        for bt, b in normal.items():
            if bt not in adapter_bases:
                bases[bt] = b
                source[f"base_by_book_type[{bt}]"] = "settings"
        for bt, b in over.items():
            bases[bt] = b
            source[f"base_by_book_type[{bt}]"] = "settings_override"
        if "TRADING" not in bases:
            bases["TRADING"] = DEC01_TRADING_BASE
            source["base_by_book_type[TRADING]"] = "DEC01"
        pol = replace(policy, base_by_book_type=bases)
        given = {k: v for k, v in self.policy.items() if v is not None}
        if given:
            s = apply_funding_policy([{"policy_id": "settings", **given}], pol)
            pol = s.policy
            source.update({k: "settings" for k in s.source})
        return pol, source


def from_doc(doc: Any, *, error: str | None = None) -> FundingSettings:
    """The effective settings of a settings.json ``funding`` object (None: the defaults). A malformed object gives the
    defaults with ``error`` set (never a guess from half of it)."""
    errors = check(doc)
    if errors:
        return from_doc(None, error="; ".join(errors))
    d = copy.deepcopy(DEFAULTS)
    stored = isinstance(doc, dict)
    for k, v in (doc or {}).items():
        if v is not None:
            d[k] = copy.deepcopy(v)
    return FundingSettings(d["default_rate"], dict(d["default_rate_by_ccy"]),
                           tuple(_row(r) for r in d["rules"] or []), dict(d["policy"] or {}), stored, error)


def load() -> FundingSettings:
    """The current funding settings of ``<home>/settings.json`` (re-read on each call, as ``desk.settings.load``)."""
    from .settings import SettingsError, read_settings_json  # settings imports this module

    try:
        doc = read_settings_json()
    except SettingsError as e:
        return from_doc(None, error=str(e))
    return from_doc(doc.get(KEY))


def options() -> dict[str, Any]:
    """What the settings form offers: the kinds, selectors and their values, and the defaults."""
    from .publish.tdm_run import ASSET_CLASS

    return {"default_kinds": [{"value": k, "text": KIND_TEXT[k]} for k in DEFAULT_KINDS],
            "rate_kinds": list(RATE_KINDS), "bases": list(BASES), "line_kinds": list(LINE_KINDS),
            "day_counts": list(DAY_COUNTS), "selectors": list(SELECTORS), "policy_fields": list(POLICY_FIELDS),
            "security_classes": sorted(set(ASSET_CLASS.values())), "defaults": DEFAULTS, "hint": HINT,
            "label": DEFAULT_RATE_LABEL, "reporting_ccy": REPORTING_CCY}
