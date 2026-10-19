"""Response models of desk API v1: the OpenAPI the S6 frontend generates TypeScript from."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

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
    secid: str
    quantity: float
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
