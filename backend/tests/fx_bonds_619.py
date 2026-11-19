"""Synthetic foreign-currency bonds of every group of TDM#619 (invented names and amounts), on the contract 1.4 golden
snapshot of 15.10.2026: a EUR and a CNY copy of its amortizing USD-face bond in each combination of

- the quote: on the business date (``today``), an earlier day's (``stale``: MOEX's legal close of the last trade day,
  the exact path's ``stale_quote``), none (``none``);
- MOEX's own yield: published (``src``) or not (``nosrc``).

The curves are the run's: CNY on the XCCY curve (in the golden snapshot), EUR on a synthetic ECB AAA par set that a run
with ``proxy_curves`` discounts on; ``without_curve`` drops it again."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

from desk.marketdata.bundle import SnapshotDir

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1_4"
DATE = "2026-10-15"
BD = date(2026, 10, 15)
TEMPLATE = "RU000A10BRM5"
"""The golden snapshot's amortizing bond of a USD face (1000, monthly coupons of 7.75 %, to 2029-11-12)."""
STALE_DATE = "2026-10-12"
AAA = (("1M", 2.10), ("3M", 2.15), ("6M", 2.20), ("1Y", 2.30), ("2Y", 2.40), ("5Y", 2.55), ("10Y", 2.85))
"""Invented ECB AAA par yields, percent (as the hub publishes them)."""
GROUPS = [(ccy, quote, src) for ccy in ("EUR", "CNY") for quote in ("today", "stale", "none")
          for src in ("src", "nosrc")]


def secid(ccy: str, quote: str, src: str) -> str:
    return f"FX{ccy}{quote[:2].upper()}{src[:2].upper()}"


def synthetic_dir(tmp: Path, *, eur_curve: bool = True, groups=GROUPS, clean: str = "97.25",
                  source_yield: str = "5.1") -> SnapshotDir:
    sd = SnapshotDir(FIX)
    market = sd.market(DATE, "eod_final")
    static = copy.deepcopy(sd.get(market.static_snapshot_id).doc)
    m = copy.deepcopy(market.doc)
    d = static["data"]
    inst = next(i for i in d["instruments"] if i["secid"] == TEMPLATE)
    q0 = next(x for x in m["data"]["bond_quotes"] if x["secid"] == TEMPLATE and x["board"] == "TQCB")
    rates = {"EUR": 93.9229, "CNY": 12.5141}  # the run's rate is MOEX 19:00 or CBR: only the accrued's scale
    for ccy, quote, src in groups:
        sid = secid(ccy, quote, src)
        d["instruments"].append({**inst, "secid": sid, "isin": sid, "face_unit": ccy, "name": f"Synthetic {sid}",
                                 "regnumber": f"4B02-{sid}"})
        for k in ("coupon_periods", "redemptions", "offers"):
            d[k] += [{**r, "secid": sid, **({"face_unit": ccy} if "face_unit" in r else {})}
                     for r in d[k] if r["secid"] == TEMPLATE]
        if quote == "none":
            continue
        sa = {**q0["source_analytics"]}
        if src == "src":
            sa.update(effective_yield_pct=source_yield, yield_close_pct=source_yield, duration_days="700")
        else:
            sa = {k: None for k in sa}
        row = {**q0, "secid": sid, "face_unit": ccy, "source_analytics": sa, "legal_close_pct": clean,
               "close_pct": clean, "waprice_pct": clean, "market_price2_pct": clean, "market_price3_pct": clean,
               "accrued_per_bond": str(round(3.4 * rates[ccy], 3)),
               "trade_date": DATE if quote == "today" else STALE_DATE}
        m["data"]["bond_quotes"].append(row)
    if eur_curve:
        qs = copy.deepcopy(next(s for s in m["data"]["curve_quote_sets"] if s["dataset"] == "cny_ruonia_xccy"))
        qs.update(dataset="eur_aaa_par", source="ecb", quote_set_id=990002, labels=["mid_only", "proxy_ecb_aaa_par"],
                  quotes=[{"tenor": t, "position": i, "mid_pct": str(v), "bid_pct": None, "ask_pct": None,
                           "tenor_days": None, "tenor_months": None, "pillar_date": None, "extra": None}
                          for i, (t, v) in enumerate(AAA)])
        m["data"]["curve_quote_sets"].append(qs)

    def put(doc):
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        sid = hashlib.sha256(raw).hexdigest()
        (tmp / f"{sid}.json.gz").write_bytes(gzip.compress(raw))
        return sid

    m["static_snapshot_id"] = put(static)
    put(m)
    return SnapshotDir(tmp)


def without_curve(ctx, ccy: str):
    """``ctx`` of a run that has no curve of ``ccy`` (the proxy not built, the XCCY quotes missing)."""
    fx = copy.copy(ctx.fx)
    fx.curves = {k: c for k, c in ctx.fx.curves.items() if c.ccy != ccy}
    return replace(ctx, fx=fx)
