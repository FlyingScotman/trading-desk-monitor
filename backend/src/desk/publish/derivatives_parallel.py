"""Derivatives valued two ways side by side (core B#107, TDM#49): ``computed`` by core from the trade terms (result
contract v3, ``valuation.derivatives``) and ``imported``, the PV the bank's report states, before a product is
switched from one to the other.

Every run writes the block ``derivatives_parallel`` to ``run_reference`` (``/api/v1/runs/{id}/derivatives``), one
row per derivative position:

- ``both``: valued by core and the report states a PV (the positions row's ``mv_rub``, else the sum of its trades'
  ``pv_rub``); ``diff_rub`` = computed - imported;
- ``computed_only``: valued by core, the report states no PV;
- ``imported_only``: not valued by core (no trade terms, or core could not: ``reason``), the report states a PV;
- ``neither``: not valued by core and no PV in the report.

``used`` says which value the run publishes: today core's whenever it values the row, the report's otherwise
(``valuation.positions``); a per-product switch comes with the products (TDM#45). A difference is ``over`` the
tolerance when it exceeds max(``abs_rub``, ``rel`` x |imported|); the tolerance is provisional (a setting once the
owner fixes it). The block holds the run's own numbers only, like ``run_position``.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

from ..orchestrator import Outcome

KIND = "derivatives_parallel"
TOLERANCE = {"abs_rub": 10_000.0, "rel": 0.01, "provisional": True}
STATUSES = ("both", "computed_only", "imported_only", "neither")


def _num(x: Any) -> float | None:
    try:
        v = float(x) if x is not None else None
    except (TypeError, ValueError):
        return None
    return v if v is not None and math.isfinite(v) else None


def _sums() -> dict[str, Any]:
    return {"n": 0, **dict.fromkeys(STATUSES, 0), "computed_rub": 0.0, "imported_rub": 0.0, "diff_rub": 0.0,
            "abs_diff_rub": 0.0, "max_abs_diff_rub": 0.0, "n_over": 0}


def compare(positions: list[tuple[str, str, Outcome]], extra: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The block of the run: ``positions`` as ``desk_run.publish`` gets them, ``extra`` the positions rows
    (``PositionInputs.extra``) for the report's PV of a row core did not value."""
    rows: list[dict[str, Any]] = []
    for pid, book, o in positions:
        v = o.valuation
        core = v.extra.get("core") if v is not None and v.extra else None
        if core:
            computed, imported = _num(v.pv_rub), _num(v.extra.get("reported_pv_rub"))  # type: ignore[union-attr]
            product, reason = core.get("product"), None
        elif o.core_not_valued:
            x = extra.get(pid) or {}
            computed, imported = None, _num(x.get("mv_rub"))
            product, reason = (x.get("asset_class") or None), o.core_not_valued[0]
        else:
            continue
        status = ("both" if imported is not None else "computed_only") if computed is not None else \
            ("imported_only" if imported is not None else "neither")
        row: dict[str, Any] = {"position_id": pid, "book_id": book, "product": product, "status": status,
                               "used": "computed" if computed is not None else "imported",
                               "computed_pv_rub": computed, "imported_pv_rub": imported}
        if reason:
            row["reason"] = reason
        if status == "both":
            assert computed is not None and imported is not None
            diff = computed - imported
            row["diff_rub"] = diff
            row["over"] = abs(diff) > max(TOLERANCE["abs_rub"], TOLERANCE["rel"] * abs(imported))
        rows.append(row)
    totals, by_book, by_product = _sums(), defaultdict(_sums), defaultdict(_sums)
    reasons: Counter[str] = Counter()
    for r in rows:
        if r.get("reason"):
            reasons[r["reason"]] += 1
        for s in (totals, by_book[r["book_id"]], by_product[r["product"] or "-"]):
            s["n"] += 1
            s[r["status"]] += 1
            if r["status"] == "both":
                s["computed_rub"] += r["computed_pv_rub"]
                s["imported_rub"] += r["imported_pv_rub"]
                s["diff_rub"] += r["diff_rub"]
                s["abs_diff_rub"] += abs(r["diff_rub"])
                s["max_abs_diff_rub"] = max(s["max_abs_diff_rub"], abs(r["diff_rub"]))
                s["n_over"] += r["over"]
    rows.sort(key=lambda r: (-abs(r.get("diff_rub") or 0.0), r["book_id"], r["position_id"]))
    return {"tolerance": TOLERANCE, "totals": totals, "by_book": dict(sorted(by_book.items())),
            "by_product": dict(sorted(by_product.items())), "reasons": dict(reasons.most_common()), "rows": rows}


def log_line(block: dict[str, Any]) -> tuple[str, str] | None:
    """The run log's line of the block, None without derivatives."""
    t = block["totals"]
    if not t["n"]:
        return None
    text = (f"деривативы, ядро и отчёт параллельно: {t['n']} поз.; обе оценки у {t['both']}, расхождение "
            f"Σ {t['diff_rub']:,.0f} ₽, Σ|Δ| {t['abs_diff_rub']:,.0f} ₽, макс. {t['max_abs_diff_rub']:,.0f} ₽, "
            f"выше допуска {t['n_over']}; только ядро {t['computed_only']}; только отчёт {t['imported_only']}"
            + (f" ({', '.join(f'{k} {n}' for k, n in block['reasons'].items())})" if block["reasons"] else "")
            + (f"; без оценки {t['neither']}" if t["neither"] else ""))
    return ("WARN" if t["n_over"] else "INFO"), text
