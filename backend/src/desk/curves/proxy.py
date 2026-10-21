"""The hub's government par-yield proxies of USD and EUR rates (contract 1.3, WEB-16): ``usd_ust_par`` (US Treasury
par yields), ``eur_aaa_par`` (ECB AAA euro-area par yields), ``eur_bund_par`` (Bundesbank, only when it served as the
EUR fallback), labelled ``proxy_ust_par`` / ``proxy_ecb_aaa_par`` / ``proxy_bund_par``. They are **not OIS** curves
(no SOFR / €STR swap quotes reach the hub): the pages show them as «прокси (UST par), не OIS» and the desk does not
compute USD/EUR rate risk on them unless the setting ``proxy_curves_for_risk`` says so (default off; then they give
the points and the day's move of the run's ``USD`` / ``EUR`` curves, ``publish.tdm_run.curves_reference``; the DV01
of a foreign bond stays its duration proxy).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

PROXIES = {"usd_ust_par": ("USD", "UST par"), "eur_aaa_par": ("EUR", "ECB AAA par"),
           "eur_bund_par": ("EUR", "Bund par")}
"""Proxy quote set -> (currency, what it is)."""
PROXY_LABELS = ("proxy_ust_par", "proxy_ecb_aaa_par", "proxy_bund_par")
PREFERENCE = {"USD": ("usd_ust_par",), "EUR": ("eur_aaa_par", "eur_bund_par")}


def is_proxy(dataset: str | None, labels: Iterable[str] | None = ()) -> bool:
    """A government par-yield proxy: one of ``PROXIES`` by name, or any set the hub labels ``proxy_*``."""
    return dataset in PROXIES or any(str(x).startswith("proxy_") for x in labels or ())


def label(dataset: str) -> str:
    """The pages' label of a proxy curve: «прокси (UST par), не OIS»."""
    what = PROXIES.get(dataset, (None, dataset))[1]
    return f"прокси ({what}), не OIS"


def years(q: Mapping[str, Any]) -> float | None:
    """The term of a quote in years: its months, else its days, else its tenor code (``3M``, ``10Y``)."""
    if q.get("tenor_months") is not None:
        return float(q["tenor_months"]) / 12
    if q.get("tenor_days") is not None:
        return float(q["tenor_days"]) / 365.25
    t = str(q.get("tenor") or "").upper()
    try:
        n, u = float(t[:-1]), t[-1]
    except (ValueError, IndexError):
        return None
    f = {"D": 1 / 365.25, "W": 7 / 365.25, "M": 1 / 12, "Y": 1.0}.get(u)
    return n * f if f else None


def points(quote_sets: Iterable[Mapping[str, Any]], ccy: str) -> tuple[str, list[tuple[float, float]]] | None:
    """(dataset, [(years, mid %)]) of the first proxy of ``ccy`` (``PREFERENCE``) a market snapshot's
    ``curve_quote_sets`` has with quotes; None without one."""
    sets = {s.get("dataset"): s for s in quote_sets}
    for ds in PREFERENCE.get(ccy, ()):
        s = sets.get(ds)
        if not s:
            continue
        pts = []
        for q in s.get("quotes") or ():
            t, m = years(q), q.get("mid_pct")
            if m is None and q.get("bid_pct") is not None and q.get("ask_pct") is not None:
                m = (Decimal(str(q["bid_pct"])) + Decimal(str(q["ask_pct"]))) / 2
            if t is not None and m is not None:
                pts.append((t, float(Decimal(str(m)))))
        if pts:
            return ds, sorted(pts)
    return None


def at(pts: list[tuple[float, float]], t: float) -> float | None:
    """Linear in the term between the quoted tenors, flat beyond the first and the last."""
    if not pts or not math.isfinite(t):
        return None
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    i = bisect.bisect_left([x for x, _ in pts], t)
    (x0, y0), (x1, y1) = pts[i - 1], pts[i]
    return y0 + (y1 - y0) * (t - x0) / (x1 - x0)
