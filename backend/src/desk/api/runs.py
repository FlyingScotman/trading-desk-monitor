"""Which run a page shows and what its header says, shared by the API's routes (``app``, ``market_history``), the
monitors' data (``concepts_run``) and the window.DESK adapter (``compat``, which re-exports them until it is removed,
TDM#107): ``resolve_run``, ``run_slice``, ``risk_notes`` and the PnL components ``PNL_KEYS``. Moved out of ``compat``
unchanged (TDM#289)."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..curves.rates import input_notes
from ..store import CURRENT_RUNS, DEFAULT_RUNS, slice_label

PNL_KEYS = ("carry", "rolldown", "rates", "spread", "fx_gross", "fx_funding", "funding", "convexity",
            "new_trades", "other")
"""The PnL components of a run position (``run_position.pnl``), in the order the explained PnL sums them."""

HEADER_SQL = (f"SELECT r.*, r.run_id IN ({DEFAULT_RUNS}) AS is_default,"
              f" r.run_id IN ({CURRENT_RUNS}) AS is_current FROM run r")
"""A run's row with its ``is_default`` / ``is_current`` flags (``RunHeader``)."""

RUONIA_DISCOUNT_VERSION = 4
"""``VALUATION_VERSION`` from which RUB bonds are discounted on RUONIA OIS (an older run has its risk on RUB OFZ)."""


def risk_notes(run: dict[str, Any]) -> list[dict[str, Any]]:
    """``riskNotes`` of a run: ``{code, curve, text, detail}`` per rates input its market lacked (``rates.input_notes``:
    ``curve_missing:ruonia_ois`` and so on) and ``run_before_ruonia`` for a run made by a logic version before the
    RUONIA discounting (only the latest positions date is recomputed after an upgrade)."""
    lin = run.get("lineage") or {}
    out = input_notes(lin.get("rates"), run["business_date"])
    m = re.search(r"\+v(\d+)", lin.get("logic_version") or "")
    if m and int(m.group(1)) < RUONIA_DISCOUNT_VERSION:
        out.append({"code": "run_before_ruonia", "curve": "RUB RUONIA", "detail": lin.get("logic_version"),
                    "text": "прогон посчитан до перехода на дисконтирование по RUONIA — риск облигаций на ОФЗ "
                            "(RUB OFZ); после обновления пересчитывается только последняя дата"})
    return out


def resolve_run(con, as_of: date | None = None, run_id: int | None = None) -> dict[str, Any] | None:
    """As tdm.assemble.resolve_run: the complete run run_id, else the run shown for as_of, else the newest run shown
    (``CURRENT_RUNS``, R11: a date's default run, otherwise its intraday run: today's book on the latest cut)."""
    if run_id:
        return con.execute("SELECT * FROM run WHERE run_id = %s AND status = 'complete'",
                           (run_id,)).fetchone()
    if as_of:
        return con.execute(f"SELECT * FROM run WHERE run_id IN ({CURRENT_RUNS}) AND business_date = %s",
                           (as_of,)).fetchone()
    return con.execute(f"SELECT * FROM run WHERE run_id IN ({CURRENT_RUNS})"
                       " ORDER BY business_date DESC LIMIT 1").fetchone()


def run_slice(run: dict[str, Any]) -> dict[str, Any]:
    """The run's market slice for ``source`` (R11): cut type and time, the label the pages show, and what the flash
    PnL is measured against when it is not the previous business day's close; ``marketDate``: the market snapshot's
    date (lineage ``market_business_date``, null for runs made before #22), older than the run's date when today's
    book is valued on an earlier close: the header shows both dates. The morning run (cut ``prev_close``, #21):
    ``marketDate`` / ``marketCutType`` from its lineage ``market_close`` (the close's date and ``eod_final`` /
    ``eod_preliminary``), its slice «16.10 · закрытие 15.10»."""
    lineage = run.get("lineage") or {}
    basis = lineage.get("pnl_basis") or {}
    close = lineage.get("market_close") or {}
    cut_ts = run.get("cut_ts")
    return {"cutType": run["cut_type"], "cutTs": cut_ts.isoformat() if cut_ts else None,
            "slice": slice_label(run["business_date"], run["cut_type"], cut_ts, run["run_type"], close or None),
            "prevCutType": run.get("prev_cut_type"), "pnlBasis": basis.get("label"),
            "marketDate": close.get("date") or lineage.get("market_business_date"),
            "marketCutType": close.get("cut_type")}
