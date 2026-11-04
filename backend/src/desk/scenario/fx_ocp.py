"""The scenario screen's FX shock from the published OCP (TDM#577, D12; the OCP of TDM#567).

The owner's rule (04.11.2026): where an open currency position comes from (a bond's revaluation or cash) does not
matter, the OCP of delta-1 instruments is simply the OCP. So the linear part of an FX shock is the run's **published**
OCP by currency (the same ``byBook`` lines the OCP screen reads, ``concepts_run.ocp_status``) x the shock, without the
rows of non-linear derivatives. Those (an option, or any position with a non-zero second derivative in an FX factor) are
shown apart by the screen: their PnL is not the OCP x the shock.

``fx_ocp`` gives, per published line, the amount in the currency, the non-linear derivatives' share of it (their FX
delta, as the OCP takes an option) and the linear rest, with the currency's rate. A run without a published OCP gives
``available`` false and the reason: the screen then falls back to the positions' FX deltas and says so. No breakdown of
the linear part by position is made (the owner: not needed)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from ..rules import ocp as O

NO_OCP = "в прогоне нет публикуемой ОВП"


def nonlinear_positions(payload: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, dict]]:
    """position_id -> (book, FX delta per currency) of the valued positions that are non-linear in FX: a non-zero
    second derivative between two FX factors (core's Hessian; an FX forward has none, only an FX x rate cross term), or
    an option by the OCP's own rule (``option`` in core's product or in the report's product group)."""
    factors = payload["factors"]
    fx_idx = {i for i, k in enumerate(factors) if k.startswith("FX|")}
    valued = [r for r in rows if r.get("fact")]
    out: dict[str, tuple[str, dict]] = {}
    for r, p in zip(valued, payload["positions"]):
        f = r["fact"]
        ex = f.get("extra") or {}
        product = str((ex.get("core") or {}).get("product") or "").lower()
        option = "option" in product or "option" in O.product_group(ex.get("reported"))
        gamma = any(g and i in fx_idx and j in fx_idx for i, j, g in p["gamma"])
        # an option with no FX exposure (a rate option in RUB) is no part of an FX shock
        if (option and any((f.get("fx_delta") or {}).values())) or gamma:
            out[p["position_id"]] = (p["book_id"], {c: x for c, x in (f.get("fx_delta") or {}).items() if x})
    return out


def fx_ocp(status: Mapping[str, Any] | None, payload: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
           amount_basis: str | None = None) -> dict[str, Any]:
    """``status``: ``concepts_run.ocp_status`` of the run (None: no published OCP); ``amount_basis``: the run's OCP
    policy (``block['policy']['amount_basis']``), None: the provisional default."""
    nl = nonlinear_positions(payload, rows)
    ids = sorted(nl)
    # the OCP takes these at their amount (nominal), not at the FX delta we subtract: the share is approximate
    basis = amount_basis or O.PROVISIONAL_POLICY.amount_basis
    approx = sorted(r["position_id"] for r in rows if r["position_id"] in nl
                    and (r["fact"].get("extra") or {}).get("ocp_amount")
                    and O.takes_ocp_amount(r["fact"].get("extra") or {}, basis))
    if not status:
        return {"available": False, "reason": NO_OCP, "as_of": None, "basis": None, "method_note": None,
                "by_ccy": {}, "by_book": [], "nonlinear_ids": ids, "approx_ids": approx, "missing_rate": []}
    by_book_ccy: dict[tuple[str, str], float] = defaultdict(float)
    for book, ccys in nl.values():
        for ccy, x in ccys.items():
            by_book_ccy[(book, ccy)] += x
    lines, tot = [], defaultdict(lambda: [0.0, 0.0])
    for x in status.get("byBook") or []:
        amount = x.get("amount")
        if amount is None:
            continue
        books = [x["book"]] if x.get("book") else list(x.get("books") or [])
        non = sum(by_book_ccy.get((b, x["ccy"]), 0.0) for b in books)
        lines.append({"book": x.get("book"), "books": books, "entity": x.get("entity"), "ccy": x["ccy"],
                      "amount": amount, "nonlinear": non, "linear": amount - non})
        tot[x["ccy"]][0] += amount
        tot[x["ccy"]][1] += non
    by_ccy = {c: {"amount": a, "nonlinear": n, "linear": a - n, "rate": (status.get("byCcy") or {}).get(c, {}).get("rate")}
              for c, (a, n) in sorted(tot.items())}
    return {"available": True, "reason": None, "as_of": status.get("asOf"), "basis": status.get("basis"),
            "method_note": status.get("methodNote"), "by_ccy": by_ccy, "by_book": lines, "nonlinear_ids": ids,
            "approx_ids": approx, "missing_rate": sorted(c for c, v in by_ccy.items() if v["rate"] is None and v["amount"])}
