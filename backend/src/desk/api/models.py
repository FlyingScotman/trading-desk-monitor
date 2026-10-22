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
    is_current: bool = Field(False, description="The run shown for its date (R11): its default run, otherwise its "
                                                "latest complete INTRADAY run (today's book on the latest intraday "
                                                "cut). Only the latest INTRADAY run of a date is kept")
    cut_ts: datetime | None = Field(None, description="Nominal UTC instant of the market cut (the intraday cut "
                                                      "time); null for runs made before R11")
    label: str = Field("", description="The run's market slice as the pages show it: «20.10 14:30 МСК · внутри "
                                       "дня», «19.10 · закрытие предв.», «19.10 · закрытие»")
    prev_cut_type: str | None = Field(None, description="Cut of the snapshot the flash PnL is measured against "
                                                        "(eod_final, else eod_preliminary); null without one")
    pnl_basis: str | None = Field(None, description="Set when the flash PnL is not against the previous business "
                                                    "day's eod_final: what it is against («PnL от 15.10 (закрытие): "
                                                    "нет закрытия 16.10»)")
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
    alias: str | None = Field(None, description="The display name a user gave fact.isin (PUT /api/v1/instruments/"
                                                "alias), in force now, old runs too; null: none, show fact.name")


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


FundingState = Literal["computed", "not_configured", "invalid_config", "failed", "not_computed"]


class FundingPeriod(BaseModel):
    start: date = Field(description="The previous business day (excluded)")
    end: date = Field(description="The run's business date (included)")
    days: int = Field(description="Calendar days accrued")


class FundingTotals(BaseModel):
    total: float = Field(description="All funding lines, RUB (a cost is negative)")
    in_positions: float = Field(description="Of it, in the positions' flash PnL (component funding)")
    book_level: float = Field(description="Of it, book-level lines: cash, closed positions, positions without PnL")
    lines: int
    positions_funded: int


class FundingWriteOff(BaseModel):
    periodicity: str | None = Field(description="daily / monthly / quarterly / none; null: not set (a problem)")
    last: date | None = Field(description="The last write-off date on or before the run's date")
    since_write_off: float | None = Field(description="Funding accrued since it, RUB (this and earlier default runs)")


class BookFunding(BaseModel):
    book_id: str
    entity: str | None
    book_type: str | None
    base: str | None = Field(description="What the book type is funded on: mv_clean, mv_dirty, cash, cost, none")
    total: float
    in_positions: float
    book_level: float
    lines: int
    by_kind: dict[str, float] = Field(description="POS, HTM, CASH, ENTITY_CASH, REPO")
    problems: int
    write_off: FundingWriteOff | None


class FundingLineOut(BaseModel):
    key: str
    kind: str
    ref: str
    book_id: str
    book_type: str
    entity: str
    ccy: str
    position_id: str | None
    base: str
    rate_expr: str
    rate_pct: float = Field(description="Rate of the period's last day, %")
    day_basis: str
    body_ccy: float = Field(description="Funded body of the last day, signed (a long bond: negative)")
    body_rub: float
    days: int
    first_day: date
    last_day: date
    interest_ccy: float
    interest_rub: float
    in_position: bool = Field(description="In the position's flash PnL (component funding)")
    note: str


class FundingProblemOut(BaseModel):
    kind: str
    ref: str
    reason: str
    book_id: str | None


class RunFunding(BaseModel):
    """The run's funding (wave B1, ``publish.funding_run``). ``state`` not_configured: the adapter gave no funding
    rules, nothing is funded and nothing is shown as 0; not_computed: a run before B1."""

    run_id: int
    state: FundingState
    text: str
    period: FundingPeriod | None
    reporting_ccy: str
    totals: FundingTotals | None
    books: list[BookFunding]
    entities: dict[str, float] = Field(description="Funding by entity, RUB")
    problems: list[FundingProblemOut]
    notes: list[str]
    provisional: list[str] = Field(description="Policy fields at the engine's provisional default (OPEN_DECISIONS)")
    policy: dict[str, Any] | None = Field(description="The FundingPolicy applied, with the source of each field")
    lines: list[FundingLineOut]


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


class RiskMissing(BaseModel):
    reason: Literal["not_valued", "reported_no_risk", "no_risk"]
    text: str
    n: int


class RiskTableRow(BaseModel):
    """One row of the table currency x instrument type (UI-02): a currency's total (``level`` ccy), a group of it
    (group) or an instrument kind (kind). DV01 is the block currency's own curves only."""
    ccy: str
    level: Literal["ccy", "group", "kind"]
    group: Literal["GOV", "SUB", "CORP", "DER", "OTH"] | None
    kind: str | None
    label: str | None
    n_positions: int = Field(description="positions of this currency (by the attribution rule)")
    n_legs_only: int = Field(description="positions of another currency with a risk here (attribution 'legs')")
    notional_ccy: float
    notional_rub: float
    mv_rub: float
    mv_ccy: float | None = Field(description="null without the currency's rate")
    dv01_rub: float = Field(description="DV01 on the curves of the row's currency, RUB per bp")
    dv01_ccy: float | None = Field(description="the same in the currency per bp; null without its rate")
    dv01_by_curve_rub: dict[str, float]
    dv01_foreign_rub: float = Field(description="risk on another currency's curves kept here (attribution "
                                    "'position'): in dv01_by_curve_rub, not in dv01_rub")
    ofz_basis_rub: float
    cs01_rub: float
    fx_delta_ccy: float
    n_risk_missing: int = Field(description="positions whose risk is missing (never counted as a zero)")
    risk_missing: list[RiskMissing]


class RiskTableTotal(BaseModel):
    """Only what sums across currencies, in RUB."""
    n_positions: int
    notional_rub: float
    mv_rub: float
    dv01_by_curve_rub: dict[str, float]
    dv01_all_curves_rub: float = Field(description="the sum over curves of different currencies: not a currency's "
                                       "risk nor a hedge measure, labelled so")
    ofz_basis_rub: float
    cs01_rub: float
    fx_delta_rub: float | None = Field(description="net FX delta at the run's rates; null when a rate is missing")
    n_risk_missing: int


class RiskTableKind(BaseModel):
    kind: str
    group: str
    label: str


class RiskTableColumn(BaseModel):
    """A column of the matrix (R38): an instrument kind present, with its total over all currencies (what sums,
    in RUB)."""
    kind: str
    group: str
    label: str
    n_positions: int
    notional_rub: float
    mv_rub: float
    dv01_by_curve_rub: dict[str, float]
    dv01_all_curves_rub: float = Field(description="the sum over curves of different currencies: labelled so")
    ofz_basis_rub: float
    cs01_rub: float
    fx_delta_rub: float | None = Field(description="net FX delta at the run's rates (RUB rows left out); null when "
                                       "a rate is missing")
    n_risk_missing: int


class RiskTableMeasure(BaseModel):
    measure: Literal["dv01", "dv01_curve", "cs01", "ofz_basis", "fx_delta", "mv", "notional", "n_positions"]
    label: str
    across_ccys: Literal["yes", "repr", "labelled"] = Field(
        description="whether the measure sums across currencies (the total row, in RUB): yes; repr only at the "
        "run's rates; labelled: sums, but over curves of different currencies, not a currency's risk")


class RiskTable(BaseModel):
    """The matrix currency x instrument type (R38): ``ccys`` its rows, ``columns`` the kinds present with their
    totals over all currencies; a cell is the ``rows`` entry of level ``kind`` (ccy, kind), a row total the entry
    of level ``ccy``, the corner ``total``."""
    run_id: int
    attribution: Literal["legs", "position"]
    attribution_rule: str
    attribution_provisional: bool = Field(description="the rule is provisional: the owner decides (UI-02)")
    cross_ccy_kinds: list[str] = Field(description="the kinds the attribution rule applies to")
    kinds: list[RiskTableKind] = Field(description="the classification's kinds, in the table's order")
    measures: list[RiskTableMeasure] = Field(description="the matrix's measures (one shown at a time)")
    fx: dict[str, float | None]
    ccys: list[str] = Field(description="the matrix's rows: currencies, RUB first")
    columns: list[RiskTableColumn] = Field(description="the matrix's columns: the kinds present, in table order")
    rows: list[RiskTableRow]
    total: RiskTableTotal


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
    published curve ("RUB RUONIA" the RUB bonds' discount curve, "RUB KEY", "RUB OFZ" the KBD, a currency's
    "USD"...), each moving only by its own vector; ofzSpread: OFZ Z-spread to RUONIA, bp; fx: % against RUB (+ = weaker rouble). Returned
    normalized: 11-pillar lists, zeros and empty entries left out."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    curves: dict[str, float | list[float]] = {}
    ofzSpread: float = 0
    spreads: SpreadRules | None = None
    fx: dict[str, float] = {}
    basis: dict[str, float] = Field(default={}, description="FX basis shift per currency, bp (core FXBASIS|<ccy>): "
                                    "exact repricing of FX derivatives only, the Taylor has no basis factor")


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
    curves: list[str] = Field(description="The curves a scenario may shock: the run's, those of the risk and "
                                          "the RUB curves core reprices (sorted)")
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
    z_spread_bp: float | None = Field(None, description="the desk run's Z-spread to its discount curve (RUONIA OIS since the owner's decision of 20.10)")
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
    moex_zspread_bp: float | None = Field(None, description="MOEX's Z-spread to the KBD")
    icpi: float | None = Field(None, description="MOEX yields ICPI (linkers), verbatim")
    ir: float | None = Field(None, description="MOEX yields IR (linkers), verbatim")
    bei: float | None = Field(None, description="MOEX yields BEI (linkers), verbatim")
    origin: str | None = Field(None, description="where the market fields come from: hub (the hub's history API) "
                               "or snapshot (a cached eod_final snapshot)")


class BondHistory(BaseModel):
    """The history of an issue for the monitors (DATA_CONTRACT §6): every quoted day of the market history
    (``md_bond_day``: the hub's history API, from 2024-01-03, else the cached hub snapshots, ``history_days`` deep)
    merged with the desk's default runs that hold it; the curve rate is the KBD at the bond's term. Empty, not 404,
    when the issue is unknown."""

    instrument_id: str | None
    isin: str | None
    name: str | None
    ccy: str | None
    curve: str | None
    spread_to_curve_note: str | None
    series: list[BondHistoryPoint]
