"""Trading Desk Monitor backend (system docs: docs/architecture/04_BACKEND.md)."""

from __future__ import annotations

import os

__version__ = "0.2.0"

# Bumped by hand when a change makes a run compute something else from the same inputs (valuation, PnL, risk
# buckets, coverage). A run records ``logic_version()`` in its lineage; the autopilot recomputes the latest positions
# date once when its default run was made by another logic version; the backfill (desk.backfill, R23) recomputes the
# older dates in the background.
VALUATION_VERSION = 10  # 2: risk per curve (R8); 3: static display fields (R9 D1-D6, D8: names, coupons, issuers);
# 4: RUB bonds discounted on RUONIA OIS + Z-spread, flash PnL on the bond's own curves;
# 5: a key-rate floater on a fallback rung books its projection on RUB KEY, the run log names missing rates inputs
# 6: a bond's offer, its next 18 months of flows and the day's quote (G-spread, volume) in extra (concept pages)
# 7: the issuer's group, sector (Russian) and state ownership from static 1.1, a bond's Latin name and issue size
# 8: the issuers' facts and the desk's ratings (issue, issuer level; extra.rating) from the newest static of the cache (R25)
# 9: core 0.33 (R26): floater accrued, G-spread to the KBD, a floater's DM and CS01 / OFZ basis by bucket from core,
# puts with their exercise windows (open_put), one core call per book; FX bonds and FX rows at the MOEX 19:00 rate
# 10: core 0.35: CNY bonds on the XCCY curve (USD/EUR on the par proxies with proxy_curves_for_risk), FX forwards,
# NDFs, swaps and crosses from trade_terms through core v3 (risk on both curves, FX delta per currency), exact accrued


def logic_version() -> str:
    """``<backend version>+v<VALUATION_VERSION>+core<core version>``, and ``+build<bundle VERSION>`` when run from the
    Windows bundle (``DESK_BUNDLE_VERSION``, set by deskctl): another value means the code that computes runs changed."""
    from rub_fixed_income import __version__ as core

    v = f"{__version__}+v{VALUATION_VERSION}+core{core}"
    build = os.environ.get("DESK_BUNDLE_VERSION", "").strip()
    return f"{v}+build{build}" if build else v
