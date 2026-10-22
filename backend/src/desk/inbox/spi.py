"""Constants of SPI v1 and the private input contract (08 §3-§6, §9, §10), shared by loader, runner and validator."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib.resources import files

SPI_VERSION = 1
SPI_MAJORS = frozenset({1})            # majors this backend can call (08 §9)
CONTRACT_MAJOR, CONTRACT_MINOR = 1, 2  # private/1.2 (08 §9: 1.1 positions risk fields; 1.2 FUTURES books, ocp,
#                                        cash_balances, pnl_official, trade_terms, reference entities and funding_rules)
CONTRACT = f"private/{CONTRACT_MAJOR}.{CONTRACT_MINOR}"
_CONTRACT_RE = re.compile(r"^private/([0-9]+)\.([0-9]+)$")

CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
AMOUNT_RE = re.compile(r"-?[0-9]+(\.[0-9]+)?")  # fullmatch: "123\n" is not an amount

# §10: every dataset of the contract; only those with a schema file can be validated by this backend yet
# (trades and internal_market have none: their sources are rejected with dataset_not_supported).
DATASETS = frozenset({"positions", "ocp", "cash_balances", "pnl_official", "trade_terms", "trades",
                      "internal_market"})
DATASETS_WITH_BOOK = DATASETS - {"internal_market"}   # a source emitting these needs reference() (§7)
MULTI_DATE_DATASETS = frozenset({"pnl_official", "trades", "cash_balances"})  # may carry _report_date (§6.1)
STOCK_DATASETS = frozenset({"positions", "ocp", "cash_balances", "trade_terms"})  # carried forward (§6.7)
DATASET_KEYS: dict[str, tuple[str, ...]] = {
    "positions": ("book_id", "position_id"),
    "ocp": ("entity", "book_id", "ccy"),
    "cash_balances": ("entity", "book_id", "ccy", "account_id", "ledger"),
    "pnl_official": ("entity", "book_id", "line_id"),
    "trade_terms": ("trade_id", "leg_id"),
    "trades": ("trade_id", "trade_version"),
}

# §7, §10: the reference tables of reference(ctx); each has a schema file. ``books`` is required when a source
# emits book_id, the others are optional (1.2). A row is keyed by REFERENCE_KEYS plus its valid_from (history).
REFERENCE_KINDS = ("books", "entities", "funding_rules")
REFERENCE_KEYS: dict[str, tuple[str, ...]] = {
    "books": ("book_id",),
    "entities": ("entity",),
    "funding_rules": ("rule_id",),
}

TIME_BASES = frozenset({"start_of_day", "end_of_day", "intraday"})
COMPLETENESS = frozenset({"full_snapshot", "increment"})
COVERS = frozenset({"all_active_books", "listed"})
SEVERITIES = frozenset({"error", "warning", "info"})
RESERVED_KEYS = frozenset({"_src", "_dataset", "_report_date"})
READ_KEYS = frozenset({"rows", "scopes", "issues", "controls", "meta"})
HIT_KEYS = frozenset({"source", "report_date", "date_from", "report_ts", "seq"})

# Package data, so wheel and bundle carry it; contract/private/v1/ is the published copy (a test keeps them equal).
SCHEMA_DIR = files("desk.inbox") / "schemas" / "v1"


def parse_contract(value: object) -> tuple[int, int] | None:
    """``private/<major>.<minor>`` as ints, or None when malformed."""
    m = _CONTRACT_RE.match(value) if isinstance(value, str) else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def contract_supported(value: object) -> bool:
    """Own major with any minor (08 §9); newer-minor fields are dropped with a warning, not rejected."""
    parsed = parse_contract(value)
    return parsed is not None and parsed[0] == CONTRACT_MAJOR


def source_datasets(decl: dict) -> list[str]:
    return list(decl["datasets"]) if decl.get("datasets") is not None else [decl["dataset"]]


def scope_keys(decl: dict, dataset: str) -> list[str]:
    scope = decl["scope"]
    return list(scope[dataset]) if isinstance(scope, dict) else list(scope)


@dataclass
class AdapterFailure(Exception):
    """A ``read`` (or ``reference``/``describe``) that raised, timed out or broke the protocol. ``type`` is the
    exception class name and ``frames`` its (file, line, function) frames: never the message (08 §6.8)."""

    kind: str                  # "failed" | "timed_out"
    type: str
    frames: list[list] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.kind}: {self.type}"
