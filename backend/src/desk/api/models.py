"""Response models of desk API v1: the OpenAPI the S6 frontend generates TypeScript from."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RunType = Literal["EOD", "INTRADAY", "TEST", "REPLAY"]
RunStatus = Literal["loading", "complete", "failed"]
COMPAT = "compat, removed with window.DESK (M7)"


class ErrorOut(BaseModel):
    code: Literal["not_found", "invalid_parameter", "store_unavailable", "internal", "unauthorized", "forbidden",
                  "locked"]
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
    rule_id: str | None = Field(default=None, description="The rule that gave the rate (of the last day); null: the "
                                                          "line's contract rate")
    rule_source: str = Field(default="adapter", description="adapter, settings, settings_override, default (the market "
                                                            "default rate of the settings), contract")
    default_rate: bool = Field(default=False, description="Funded at the market default rate («рыночная ставка по "
                                                          "умолчанию»), not at a transfer rate")


class FundingProblemOut(BaseModel):
    kind: str
    ref: str
    reason: str
    book_id: str | None


class FxLiabilityPosition(BaseModel):
    """A foreign-currency bond's currency OCP liability (``publish.fx_liability``, OQ-04, OQ-17, TDM#54, TDM#266)."""

    state: Literal["computed", "none", "problem", "not_configured", "invalid_config"] = Field(
        description="computed: its FX delta is net of the liability; otherwise the FX delta is the dirty MV and "
                    "``reason`` says why")
    reason: str | None = None
    ccy: str | None = None
    amount_ccy: float | None = Field(None, description="The liability, signed (a long bond: negative)")
    reference: Literal["prev_dirty_mv", "purchase_cost", "current_dirty_mv"] | None = Field(
        None, description="What the liability is: the dirty MV of the previous business day (the last OCP transfer), "
                          "the purchase cost (a position new on the day), today's dirty MV (no previous day: OCP 0)")
    reference_mv_ccy: float | None = Field(None, description="That dirty MV (or cost) in the currency, signed")
    reference_date: str | None = Field(None, description="The previous business day, for prev_dirty_mv")
    reference_text: str | None = None
    clean_mv_ccy: float | None = None
    accrued_ccy: float | None = None
    dirty_mv_ccy: float | None = None
    coupons_ccy: float | None = Field(None, description="Coupons received on the day in the currency, in the OCP")
    base: str | None = Field(None, description="The funding base (the rate's body, DEC01); not the liability's")
    fx_delta_gross: float | None = Field(None, description="FX delta before the liability: the dirty MV")
    fx_delta_net: float | None = Field(None, description="The published FX delta (OCP): dirty MV + liability + "
                                                         "coupons of the day, the FX PnL since the last transfer")
    fx_funding: float | None = Field(None, description="FX revaluation of the liability held over the day (the D-1 "
                                                       "dirty MV) in the PnL, RUB")
    fx_funding_cbr: float | None = None
    note: str | None = None


class FxLiability(BaseModel):
    """The currency OCP liability of the run's foreign-currency bonds; ``provisional``: what the rule takes until the
    data has more (the residual of past days does not accumulate)."""

    status: str
    text: str
    rule: str
    provisional: list[str]
    by_ccy: dict[str, float] = Field(description="Liabilities by currency, signed")
    positions: dict[str, FxLiabilityPosition]
    notes: list[str]


class RunFunding(BaseModel):
    """The run's funding (wave B1, ``publish.funding_run``). ``state`` not_configured: no funding rule (adapter
    or settings) and the market default rate off, nothing is funded and nothing is shown as 0; not_computed: a run
    before B1."""

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
    default_rate: dict[str, Any] | None = Field(default=None, description="The market default rate of the settings in "
                                                "force (by currency), and how many lines and positions it funded")
    fx_liability: FxLiability | None = Field(None, description="The bonds' currency OCP liability (TDM#54, #266); "
                                                               "null for a run before it")


class ParallelRow(BaseModel):
    """One derivative position valued two ways (``publish.derivatives_parallel``)."""

    position_id: str
    book_id: str
    product: str | None = Field(description="Core's product (fx_forward, fx_swap, fx_ndf, fx_cross, fx_future; "
                                "several joined by +), else <asset_class>_other")
    status: Literal["both", "computed_only", "computed_invalid", "imported_only", "neither"]
    used: Literal["computed", "imported"] = Field(description="The value the run publishes")
    computed_pv_rub: float | None = Field(description="PV by core from the trade terms (result v3)")
    imported_pv_rub: float | None = Field(description="PV the report states (mv_rub, else the trades' pv_rub)")
    diff_rub: float | None = None
    over: bool | None = Field(None, description="diff above the tolerance")
    reason: str | None = Field(None, description="Why core did not value the row (pv_not_finite: core's PV is not a "
                                                 "finite number)")


class RunDerivatives(BaseModel):
    """Derivatives computed by core next to the report's PV, before a product is switched (TDM#49). ``state``
    not_computed: a run before this block; sums are per status, money sums over the rows with both values."""

    run_id: int
    state: Literal["computed", "not_computed"]
    tolerance: dict[str, Any] | None
    totals: dict[str, Any] | None
    by_book: dict[str, dict[str, Any]]
    by_product: dict[str, dict[str, Any]]
    reasons: dict[str, int]
    rows: list[ParallelRow]


OcpState = Literal["computed", "no_report", "failed", "not_computed"]


class OcpLineOut(BaseModel):
    key: str = Field(description="position_id, CASH|<book>|<ccy>|<account>, FUND|<position_id>")
    component: str = Field(description="cash, fx_legs, futures, bonds, funding, other, transfers")
    book_id: str
    entity: str
    ccy: str
    amount: float = Field(description="Signed, units of the currency: + long")
    position_id: str | None
    basis: str = Field(description="spot_equivalent, nominal, notional, amount, balance, liability, mv")
    note: str


class OcpComparison(BaseModel):
    """The reported OCP against the OCP derived from positions, at the level of the report (a book or an entity)."""

    level: Literal["book", "entity"]
    entity: str
    book_id: str | None
    ccy: str
    reported: float = Field(description="ocp_ccy of the report (0: a covered scope without a row of the currency)")
    reported_rub: float | None
    reported_rate: float | None
    in_report_file: bool = Field(description="The report has a row of this scope and currency")
    src: list[str]
    derived: float = Field(description="All components")
    components: dict[str, float]
    in_report: float = Field(description="The components the report holds (policy report_components)")
    not_in_report: dict[str, float] = Field(description="The components outside the report: the explained part")
    break_: float = Field(alias="break", description="reported - in_report")
    break_rub: float | None = None
    over: bool = Field(description="|break| above the tolerance")
    hints: list[str] = Field(description="Which component (or sign) would close the break")
    set_id: int | None
    valid_date: date | None

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)


class OcpTransferDeal(BaseModel):
    position_id: str | None
    amount: float
    trade_ids: list[str]
    counterparty: str | None
    rate: float | None
    value_date: date | None


class OcpTransfer(BaseModel):
    book_id: str
    entity: str | None
    ocp_to: str | None = Field(description="The book's ocp_to; a receiving book: the deals' counterparty book")
    source: bool = Field(description="The book transfers its OCP (its ocp_to is set); false: it receives")
    ccy: str
    before: float = Field(description="The book's derived OCP without the transfer deals")
    transferred: float
    after: float
    expected: float | None = Field(description="transfer_target full: -before")
    gap: float | None = Field(description="What the transfer left (after)")
    over: bool
    rate: float | None
    value_date: date | None
    deals: list[OcpTransferDeal]


class RunOcp(BaseModel):
    """The run's OCP (wave B2, TDM#59, ``publish.ocp_run``): derived from positions by component, reconciled with the
    reported ``ocp`` and the OCP transfers. ``state`` no_report: no ``ocp`` unit in force (derived only);
    not_computed: a run before B2. ``policy.provisional``: the conventions at a provisional default (TDM#60)."""

    run_id: int
    state: OcpState
    text: str
    as_of: date | None
    reporting_ccy: str
    components: list[dict[str, str]]
    policy: dict[str, Any] | None
    by_ccy: dict[str, dict[str, Any]] = Field(description="The desk's derived OCP: components, total, rate, total_rub")
    by_book: list[dict[str, Any]]
    lines: list[OcpLineOut]
    reconciliation: list[OcpComparison]
    outside_report: list[dict[str, Any]] = Field(description="Derived OCP of books no report unit covers")
    transfers: list[OcpTransfer]
    report: dict[str, Any] = Field(description="The report's units in force and its number of rows")
    totals: dict[str, Any] | None
    notes: list[str]


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


OFZ_BASIS = ("the OFZ basis to RUONIA OIS, RUB per bp, in the ofz_view 'basis' only (0 in 'kbd'): since 0.2.7 a fixed "
             "OFZ's risk to the KBD's par yields with RUONIA held (its KBD par risk), computed apart from its RUONIA "
             "risk in dv01_by_curve_rub['RUB RUONIA'] (TDM#213; in 0.2.6 the same risk again); a second factor, as "
             "CS01: never add it to the DV01. An OFZ-PK's spread to RUONIA; before 0.2.6, every OFZ's spread to RUONIA "
             "(ofz_spr01). Never an OFZ-IN's nominal proxy (dv01_ofz_in_proxy_rub)")
OFZ_SPREAD_KBD = ("a fixed OFZ's own spread risk to the KBD (0.2.6), RUB per bp; in the ofz_view 'kbd' also an OFZ-PK's "
                  "spread to RUONIA (its basis in 'basis'); 0 in a run before 0.2.6")
BY_CURVE = ("DV01 by curve, RUB per bp: the monitors' curve ids; an OFZ's KBD risk on RUB OFZ in the ofz_view 'kbd', its "
            "RUONIA risk on RUB RUONIA in 'basis' (since 0.2.7 computed apart from the basis; in 0.2.6 its KBD risk)")
OFZ_IN_PROXY = ("of which an OFZ-IN's nominal-rate proxy, RUB per bp (TDM#173): a linker core does not value yet (TDM#38), "
                "its duration on the KBD. Already in dv01_by_curve_rub (RUB RUONIA in the ofz_view 'basis', RUB OFZ in "
                "'kbd') and dv01_rub, never in ofz_basis_rub: a memo, not a risk to add")


class RiskTableRow(BaseModel):
    """One row of the table currency x instrument type (UI-02): a currency's total (``level`` ccy), a group of it
    (group) or an instrument kind (kind). DV01 is the block currency's own curves only."""
    ccy: str
    level: Literal["ccy", "group", "kind"]
    group: Literal["GOV", "SUB", "CORP", "DER", "MM", "OTH"] | None
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
    dv01_by_curve_rub: dict[str, float] = Field(description=BY_CURVE)
    dv01_foreign_rub: float = Field(description="risk on another currency's curves kept here (attribution "
                                    "'position'): in dv01_by_curve_rub, not in dv01_rub")
    ofz_basis_rub: float = Field(description=OFZ_BASIS)
    ofz_spread_kbd_rub: float = Field(description=OFZ_SPREAD_KBD)
    dv01_ofz_in_proxy_rub: float = Field(description=OFZ_IN_PROXY)
    cs01_rub: float
    fx_delta_ccy: float
    n_risk_missing: int = Field(description="positions whose risk is missing (never counted as a zero)")
    risk_missing: list[RiskMissing]


class RiskTableTotal(BaseModel):
    """Only what sums across currencies, in RUB."""
    n_positions: int
    notional_rub: float
    mv_rub: float
    dv01_by_curve_rub: dict[str, float] = Field(description=BY_CURVE)
    dv01_all_curves_rub: float = Field(description="the sum over curves of different currencies: not a currency's "
                                       "risk nor a hedge measure, labelled so")
    ofz_basis_rub: float = Field(description=OFZ_BASIS)
    ofz_spread_kbd_rub: float = Field(description=OFZ_SPREAD_KBD)
    dv01_ofz_in_proxy_rub: float = Field(description=OFZ_IN_PROXY)
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
    dv01_by_curve_rub: dict[str, float] = Field(description=BY_CURVE)
    dv01_all_curves_rub: float = Field(description="the sum over curves of different currencies: labelled so")
    ofz_basis_rub: float = Field(description=OFZ_BASIS)
    ofz_spread_kbd_rub: float = Field(description=OFZ_SPREAD_KBD)
    dv01_ofz_in_proxy_rub: float = Field(description=OFZ_IN_PROXY)
    cs01_rub: float
    fx_delta_rub: float | None = Field(description="net FX delta at the run's rates (RUB rows left out); null when "
                                       "a rate is missing")
    n_risk_missing: int


class RiskTableMeasure(BaseModel):
    measure: Literal["dv01", "dv01_curve", "cs01", "ofz_basis", "ofz_spread_kbd", "fx_delta", "mv", "notional",
                     "n_positions"]
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
    ofz_view: Literal["kbd", "basis"] = Field(description="how an OFZ's risk is shown (TDM#173, TDM#213): kbd, its KBD "
                                              "par risk on RUB OFZ with no basis (every field adds up once); basis, "
                                              "its RUONIA risk on RUONIA plus the OFZ basis apart (ofz_basis_rub: "
                                              "another factor, as CS01, not to add to the DV01)")
    ofz_view_rule: str
    ofz_view_note: str | None = Field(description="set when the view is approximate (a run before 0.2.6 in 'kbd'; in "
                                                  "'basis' an OFZ whose RUONIA risk is its basis again: a run of 0.2.6, "
                                                  "an OFZ off core)")
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
    published curve ("RUB RUONIA" the RUB bonds' discount curve, "RUB KEY", "RUB OFZ" a shift of the OFZ yields,
    the KBD's par yields (TDM#226: an OFZ in RUONIA + basis moves its basis by RUB OFZ - RUB RUONIA), a currency's
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
    issuer_group: str | None = Field(None, description="The issuer's group: the hub's group of the issuer, else its "
                                                        "display name; null without an issuer (TDM#72)")
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


# ---- the forward carry (TDM#66, ``scenario.carry``): camelCase, as the concept pages' bodies
CarryFundingState = Literal["funded", "not_funded", "problem", "not_configured", "not_valued"]
CarrySource = Literal["adapter", "settings", "settings_override", "default", "contract", "none"]


class CarryCcyAmounts(BaseModel):
    """A position's amounts in its own currency (``CarryPosition.inCcy``): the RUB amounts at the run's rate
    (``fxRate``, held to the horizon); the funding the lines' own interest when they are in that currency."""

    carry: float | None
    coupon: float | None
    pull: float | None
    rolldown: float | None
    spreadRoll: float | None
    funding: float | None
    net: float | None


class CarryPosition(BaseModel):
    """One position's carry to the horizon, RUB, signed (``inCcy``: in its currency).
    ``carry = coupon + pull + rolldown`` (core ``horizon_portfolio``, the curves carried), ``+ spreadRoll`` with
    spread=curve; null with ``code`` when there is no forecast."""

    positionId: str
    bookId: str
    entity: str | None
    secid: str | None
    ccy: str | None
    kind: str | None = Field(description="Instrument kind of the risk table (``concepts.instr_kind``)")
    quality: str | None
    carry: float | None = Field(description="The whole change of value to the horizon (coupons received + the "
                                "change of the dirty price, the curves carried): coupon + pull + rolldown (core "
                                "horizon_pnl_rub) + spreadRoll; the screen's one «Кэрри» column")
    coupon: float | None = Field(description="Coupons received + the change of the accrued coupon to the horizon")
    pull: float | None = Field(description="The rest of core's carry_rub: the clean price's drift on today's forwards "
                               "at the held spread (the pull to par)")
    rolldown: float | None = Field(description="Core's rolldown_rub: the curve keeping its shape")
    funding: float | None = Field(description="Funding over (run date, horizon], RUB (negative: a cost); 0 when the "
                                  "position is not funded on its MV, null when unknown or not configured")
    net: float | None = Field(description="carry + funding; null while either is")
    dv01: float | None = Field(None, description="The run row's DV01, RUB per bp (as the monitor shows it); null "
                               "for a position the run did not value")
    cs01: float | None = Field(None, description="The run row's spread risk, RUB per bp: its CS01 (credit), else its "
                               "OFZ spread risk (ofz_spr01); for a floater the spread is its DM's")
    fxRate: float | None = Field(None, description="RUB per unit of ccy the run used (its mv_rub / mv; 1 for RUB), "
                                 "held to the horizon: inCcy = the RUB amounts / fxRate")
    inCcy: CarryCcyAmounts | None = Field(None, description="carry, coupon, pull, rolldown, spreadRoll, funding, net "
                                          "in the position's currency (ccy), beside the RUB fields")
    fundingState: CarryFundingState
    fundingSource: CarrySource = Field(description="The rule that priced the funding (the line's rule_source, "
                                       "TDM#237): adapter, settings, settings_override, default (the settings' "
                                       "market default rate), contract (an HTM lot's own rate); none without a line")
    fundingRate: str | None = None
    fundingRuleId: str | None = Field(None, description="The rule's id (the line's rule_id)")
    fundingNote: str | None = None
    spreadRoll: float | None = Field(None, description="spread=curve: CS01 x spreadRollBp (first order), included in "
                                     "carry; null when the spread is held (spreadCurve says why) or spread=held")
    spreadRollBp: float | None = Field(None, description="The spread's roll along its peers' curve to the horizon, bp: "
                                       "curve(tenor at the horizon) - curve(tenor now)")
    spreadCurve: str | None = Field(None, description="spread=curve: the rating grade whose peer curve it rolled on "
                                    "(«A (для A+)»: a neighbouring grade's), or why the spread is held: no_rating, "
                                    "no_peer_curve, ofz_held, floater_held, not_rub, redeemed, no_cs01, no_quote, "
                                    "not_covered")
    carryMethod: Literal["core", "redeemed"] | None = Field(None, description="core: core's horizon_portfolio; "
                                                            "redeemed: the bond's last flow is paid by the horizon, "
                                                            "carried as cash (received - the start's dirty PV, no "
                                                            "roll-down); null without a forecast")
    code: str | None = Field(description="Why there is no carry forecast: the quality, not_a_bond, base_mismatch, "
                             "core_<code>; null when covered")
    detail: str | None = None


class CarrySums(BaseModel):
    """A group's sums, RUB. ``net = carry + funding + fundingNoCarry + fundingBookLevel`` (= ``carry +
    fundingTotal``; a null part counts 0) whenever ``net`` is not null."""

    carry: float | None = Field(description="Sum over the covered positions (coupon + pull + rolldown + spreadRoll)")
    coupon: float | None
    pull: float | None
    rolldown: float | None
    spreadRoll: float | None
    dv01: float | None = Field(None, description="Sum of the positions' DV01 (every position that has one)")
    cs01: float | None = Field(None, description="Sum of the positions' cs01 (every position that has one)")
    funding: float | None = Field(description="Funding of the covered positions (those with a carry forecast)")
    fundingNoCarry: float | None = Field(description="Funding of the positions without a carry forecast; null when "
                                         "one of them has an unknown funding (nNoCarryNoFunding)")
    fundingBookLevel: float | None = Field(description="Funding lines without a position (cash balances, an "
                                           "entity's cash): a book, an entity, a currency and the total have them; "
                                           "null while the funding is not computed")
    fundingTotal: float | None = Field(None, description="funding + fundingNoCarry + fundingBookLevel; null while "
                                       "the funding is not computed or fundingNoCarry is null")
    net: float | None = Field(description="carry + fundingTotal; null (rule B1) while the funding is not computed, "
                              "nothing is covered, or any position's funding is unknown (nCoveredNoFunding, "
                              "nNoCarryNoFunding)")
    netKnown: float | None = Field(None, description="Sum of carry + funding of the positions that have both: the "
                                   "net of what is known when net is null")
    nPositions: int
    nCovered: int
    nNotCovered: int
    nFunded: int
    nFundingMissing: int
    nCoveredNoFunding: int = Field(0, description="Covered positions whose funding is unknown (net is then null)")
    nNoCarryNoFunding: int = Field(0, description="Positions without a carry forecast whose funding is unknown: then "
                                   "fundingNoCarry, fundingTotal and net are null (rule B1 applied strictly)")


class CarryMeeting(BaseModel):
    date: date
    effective: date
    estimated: bool
    stepBp: float = Field(description="The key-rate step requested for this meeting, bp (the steps parameter maps to "
                          "the meetings in this order; 0 with ks=flat or none given); applied from the effective "
                          "date when inHorizon")
    inHorizon: bool = Field(True, description="The meeting takes effect by the horizon (its step applies)")


class CarryOut(BaseModel):
    """The forward carry of a run to a horizon (TDM#66, ``scenario.carry``): «Кэрри» (coupon, pull, roll-down) and
    «Фондирование» apart, «Нетто» their sum (TDM#168), per position and by book, entity, currency and kind."""

    method: str
    runId: int
    asOf: date
    horizonDate: date
    days: int
    reportingCcy: str
    ks: Literal["flat", "steps"]
    spread: Literal["held", "curve"]
    spreadCurves: list[dict[str, Any]] = Field(description="spread=curve: the peer curves by rating grade (grade, "
                                               "issues, points [tenor years, median G-spread bp])")
    meetings: list[CarryMeeting] = Field(description="The CBR meetings that take effect within a year of asOf (or to "
                                         "the horizon when later), in order; steps map to them in this order, those "
                                         "with inHorizon false do not apply")
    keyRatePath: list[dict[str, Any]] = Field(description="The key rate from each date on, %: the run date's, then "
                                              "after each step (ks=steps)")
    fundingStatus: Literal["computed", "not_configured", "invalid_config", "failed"]
    fundingText: str
    positions: list[CarryPosition]
    totals: CarrySums
    byBook: dict[str, CarrySums]
    byEntity: dict[str, CarrySums]
    byCcy: dict[str, CarrySums]
    byKind: dict[str, CarrySums]
    fundingBookLevel: list[dict[str, Any]] = Field(description="Funding lines without a position (cash balances)")
    fundingProblems: list[dict[str, Any]]
    provisional: list[str] = Field(description="Funding policy fields at the engine's provisional default")
    notes: list[str] = Field(description="The assumptions of the forecast")
