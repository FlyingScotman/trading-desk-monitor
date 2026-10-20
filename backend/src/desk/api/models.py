"""Response models of desk API v1: the OpenAPI the S6 frontend generates TypeScript from."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RunType = Literal["EOD", "INTRADAY", "TEST", "REPLAY"]
RunStatus = Literal["loading", "complete", "failed"]
COMPAT = "compat, removed with window.DESK (M7)"


class ErrorOut(BaseModel):
    code: Literal["not_found", "invalid_parameter", "store_unavailable", "internal"]
    message: str
    request_id: str
    details: Any = None


class RunHeader(BaseModel):
    run_id: int
    run_type: RunType
    business_date: date
    prev_date: date | None
    cut_type: str = Field(description="eod_preliminary / eod_final / intraday")
    status: RunStatus
    started_at: datetime
    completed_at: datetime | None
    supersedes_run_id: int | None
    is_default: bool = Field(description="The default run of its date: the latest complete EOD run on "
                                         "eod_final, otherwise on eod_preliminary (04 §3)")
    market_snapshot_id: str
    static_snapshot_id: str
    coverage: dict[str, Any] | None = Field(description="{book: {quality: n}, _total: {...}}")
    totals: dict[str, Any] | None = Field(description="{book: {pv_rub, dv01_rub, cs01_rub, pnl}, _total}")
    as_of: date = Field(description=f"= business_date; {COMPAT}")
    published_at: datetime | None = Field(description=f"= completed_at; {COMPAT}")


class RunList(BaseModel):
    runs: list[RunHeader]
    next_cursor: int | None = Field(description="Pass as cursor for the next page; null on the last page")


class LogLine(BaseModel):
    seq: int
    level: Literal["INFO", "WARN", "ERROR"]
    message: str


class RunDetail(RunHeader):
    error: str | None
    lineage: dict[str, Any] = Field(description="EodContext.lineage() plus the run's lineage columns (04 §3)")
    log: list[LogLine]


class Position(BaseModel):
    position_id: str
    book_id: str
    secid: str | None = Field(description="null: no instrument (an FX, OTC or unmapped row; fact says what)")
    quantity: float | None = Field(description="Number of bonds; null for an FX row or a bond given by face only")
    quality: str | None = Field(description="null: not valued (no data at all); reason says why")
    reason: str | None
    pv_rub: float | None
    dv01_rub: float | None
    cs01_rub: float | None
    fact: dict[str, Any] | None = Field(description="The published position record (tdm position row format)")
    pnl: dict[str, Any] | None = Field(description="Flash PnL components plus extra; null without PnL")
    no_pnl: dict[str, Any] | None = Field(description="{reason, detail} when priced but without PnL")


class PositionList(BaseModel):
    run_id: int
    positions: list[Position]


class PnlSums(BaseModel):
    """Sums over the positions with a flash PnL; null when none has one."""

    carry: float | None
    rolldown: float | None
    rates: float | None
    spread: float | None
    fx_gross: float | None
    fx_funding: float | None
    funding: float | None
    convexity: float | None
    new_trades: float | None
    other: float | None
    total: float | None
    n_positions: int = Field(description="Valued positions")
    n_pnl: int = Field(description="Of those, with a flash PnL")


class PositionPnl(BaseModel):
    position_id: str
    book_id: str
    components: dict[str, Any] | None = Field(description="Flash PnL components (as_tdm); null without PnL")
    total: float | None
    extra: dict[str, Any] | None = Field(description="Method, flags and checks of the flash PnL")
    no_pnl: dict[str, Any] | None


class RunPnl(BaseModel):
    run_id: int
    positions: list[PositionPnl]
    totals: dict[str, PnlSums] = Field(description="Per book and _total")


class AggregateRow(BaseModel):
    key: dict[str, str | None]
    n_positions: int
    n_valued: int
    n_pnl: int
    pv_rub: float | None
    dv01_rub: float | None
    cs01_rub: float | None
    pnl: dict[str, float | None] = Field(description="Sums of the PnL components and total; null if none")


class Aggregates(BaseModel):
    run_id: int
    dims: list[str]
    rows: list[AggregateRow]


class LatestRun(BaseModel):
    run_id: int
    business_date: date
    completed_at: datetime
    age_seconds: float


class Ready(BaseModel):
    status: Literal["ok"]
    schema_version: str | None
    latest_run: LatestRun | None
    adapter: str | None = Field(None, description="the private adapter: 'ok <name> <version> <fingerprint[:12]>' "
                                "or 'error <reason>' (ingestion off); null when not reported")


# ---- scenarios (S6)

class SpreadRules(BaseModel):
    """Credit spread shocks in bp, additive; mult scales the position's own spread (factor > 0)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)  # "_all"/"_mult" only, not the names
    all: float | None = Field(None, alias="_all")
    mult: float | None = Field(None, alias="_mult")
    sector: dict[str, float] = {}
    issuer: dict[str, float] = {}
    rating: dict[str, float] = {}


class ScenarioDefinition(BaseModel):
    """The prototype scenario shape. curves: bp per RISK_BUCKETS pillar (11) or one number for all, per
    published curve ("RUB OFZ"); ofzSpread: OFZ Z-spread to the KBD, bp; fx: % against RUB (+ = weaker
    rouble). Returned normalized: 11-pillar lists, zeros and empty entries left out."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    curves: dict[str, float | list[float]] = {}
    ofzSpread: float = 0
    spreads: SpreadRules | None = None
    fx: dict[str, float] = {}


class ScenarioDefOut(BaseModel):
    scenario_id: str
    name: str
    kind: Literal["preset", "historical", "custom"]
    description: str | None
    ord: int
    definition: ScenarioDefinition
    definition_hash: str


class ScenarioDefList(BaseModel):
    scenarios: list[ScenarioDefOut]


class RiskPosition(BaseModel):
    position_id: str
    book_id: str
    secid: str | None
    name: str | None
    quality: str
    issuer: str | None
    sector: str | None
    rating: str | None
    spread_kind: Literal["ofz", "credit"] | None
    spread_bp: float | None
    pv_rub: float
    delta: list[tuple[int, float]] = Field(description="[factor index, RUB per unit]: IR and CS per bp, "
                                                       "FX per 1 %; nonzero only")
    gamma: list[tuple[int, int, float]] = Field(description="[i, j, RUB per unit_i x unit_j], i <= j")


class RiskPayload(BaseModel):
    """First-order and Hessian sensitivities of a run's valued positions, for the Taylor (T1) scenario
    estimate: x_i shock per factor, total = sum delta x + sum_(i<=j) (i == j ? 1/2 : 1) gamma x_i x_j."""

    run_id: int
    business_date: date
    buckets: list[str]
    curves: list[str]
    factors: list[str] = Field(description="IR|<curve>|<bucket>, then CS, then FX|<ccy>")
    positions: list[RiskPosition]


class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_run_id: int
    definition: ScenarioDefinition


class TaylorParts(BaseModel):
    rates: float
    spread: float
    fx: float
    convexity: float
    total: float


class ResultPosition(BaseModel):
    position_id: str
    book_id: str
    quality: str
    taylor: TaylorParts
    exact: float | None = Field(description="Exact repricing PnL, RUB; null when not covered")
    residual: float | None = Field(description="exact - taylor.total")
    code: str | None = Field(description="Why not covered: the fallback quality, core_<code>, base_mismatch")


class ResultTotals(BaseModel):
    taylor: float
    taylor_covered: float = Field(description="Taylor total of the positions with an exact value")
    exact: float | None
    residual: float | None
    blended: float = Field(description="exact of the covered positions + taylor of the others")
    n_positions: int
    n_exact: int


class MissingPosition(BaseModel):
    position_id: str
    reason: str


class ResultCoverage(BaseModel):
    status: Literal["complete", "partial"]
    n_positions: int
    n_exact: int
    missing: list[MissingPosition]


class ScenarioResult(BaseModel):
    method: str
    definition_hash: str
    coverage: ResultCoverage
    totals: dict[str, ResultTotals] = Field(description="Per book and _total")
    positions: list[ResultPosition]


class ScenarioJob(BaseModel):
    job_id: int
    base_run_id: int
    definition_hash: str
    definition: ScenarioDefinition
    status: Literal["queued", "running", "done", "failed"]
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    result: ScenarioResult | None


class BondHistoryPoint(BaseModel):
    date: date
    clean_price: float | None = Field(None, description="% of face; basis in price_basis")
    ytm: float | None = Field(None, description="%, effective annual: MOEX's to yield_date, else the run's")
    z_spread_bp: float | None = Field(None, description="the desk run's Z-spread to the KBD")
    g_spread_bp: float | None = Field(None, description="MOEX's")
    accrued: float | None = None
    volume_rub: float | None = None
    source: str | None = Field(None, description="market, market + desk run, desk run")
    face_amount: float | None = Field(None, description="the desk's face in the bond's currency, all books; null "
                                      "on a day without a default run")
    tenor_y: float | None = Field(None, description="years to yield_date (offer or maturity), else to maturity")
    curve_rate: float | None = Field(None, description="%, the KBD of the date at tenor_y (RUB bonds with known "
                                     "coupons)")
    spread_to_curve_bp: float | None = Field(None, description="ytm - curve_rate")
    price_basis: str | None = Field(None, description="legal_close, close or waprice (the runs' order)")
    yield_date: date | None = Field(None, description="the date MOEX's yield runs to: the offer or maturity")


class BondHistory(BaseModel):
    """The history of an issue for the monitors (DATA_CONTRACT §6): every quoted day of the market history
    (``md_bond_eod``: the cached hub snapshots, ``history_days`` deep) merged with the desk's default runs that hold
    it; the curve rate is the KBD at the bond's term. Empty, not 404, when the issue is unknown."""

    instrument_id: str | None
    isin: str | None
    name: str | None
    ccy: str | None
    curve: str | None
    spread_to_curve_note: str | None
    series: list[BondHistoryPoint]
