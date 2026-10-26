"""Trading Desk Monitor backend (system docs: docs/architecture/04_BACKEND.md)."""

from __future__ import annotations

import os

__version__ = "0.2.7"

# Bumped by hand when a change makes a run compute something else from the same inputs (valuation, PnL, risk
# buckets, coverage). A run records ``logic_version()`` in its lineage; the autopilot recomputes the latest positions
# date once when its default run was made by another logic version; the backfill (desk.backfill, R23) recomputes the
# older dates in the background.
VALUATION_VERSION = 15  # 2: risk per curve (R8); 3: static display fields (R9 D1-D6, D8: names, coupons, issuers);
# 4: RUB bonds discounted on RUONIA OIS + Z-spread, flash PnL on the bond's own curves;
# 5: a key-rate floater on a fallback rung books its projection on RUB KEY, the run log names missing rates inputs
# 6: a bond's offer, its next 18 months of flows and the day's quote (G-spread, volume) in extra (concept pages)
# 7: the issuer's group, sector (Russian) and state ownership from static 1.1, a bond's Latin name and issue size
# 8: the issuers' facts and the desk's ratings (issue, issuer level; extra.rating) from the newest static of the cache (R25)
# 9: core 0.33 (R26): floater accrued, G-spread to the KBD, a floater's DM and CS01 / OFZ basis by bucket from core,
# puts with their exercise windows (open_put), one core call per book; FX bonds and FX rows at the MOEX 19:00 rate
# 10: core 0.35: CNY bonds on the XCCY curve (USD/EUR on the par proxies with proxy_curves_for_risk), FX forwards,
# NDFs, swaps and crosses from trade_terms through core v3 (risk on both curves, FX delta per currency), exact accrued
# 11: batch 1 item 5: a foreign bond's flash PnL decomposed (on core: in its currency on its own curve, the rate move
# on the D-1 PV in fx_gross; on the fallback ladder: its carry at its yield), not all in other; funding in the runs
# (B1, publish.funding_run): the PnL component funding (the carry net of it), a funding block
# 12: core 0.37 (#32): fixed-coupon OFZ discounted on the KBD (rub_ofz_kbd_v3, par buckets 1Y-20Y+), ofz_spr01 their
# spread to the KBD, extra.ofz_basis01 the par-par basis; the fallback duration DV01 / (1 + y) (#36)
# 13: IRS / OIS on RUONIA and the key rate and REPO from trade_terms 1.4 through core v3 (#45): PV and rates risk per
# curve (RUB RUONIA, RUB KEY) instead of the report's DV01 in one bucket, the run's fixings read by the started swaps
# 14: core 0.39 (#213): an OFZ on the KBD in RUONIA + basis, both risks computed (RUONIA OIS quotes with the basis
# held; the basis = the KBD's par yields with RUONIA held), the row in the scenarios' coordinates (extra.ofz_ruonia01),
# fixed flows in core's closed form; the exact scenario names every curve of the run (#220)
# 15: every RUB bond priced at the day's price has its Z-spread to the KBD in extra (z_kbd_bp, TDM#232), beside its Z to
# the discount curve and core's G-spread


def logic_version() -> str:
    """``<backend version>+v<VALUATION_VERSION>+core<core version>``, and ``+build<bundle VERSION>`` when run from the
    Windows bundle (``DESK_BUNDLE_VERSION``, set by deskctl): the program that made a run. Whether two values compute
    the same numbers is ``backfill.same_logic``: only ``VALUATION_VERSION`` and the core version count, so a release
    without changes to the computation does not recompute the history (issue #24)."""
    from rub_fixed_income import __version__ as core

    v = f"{__version__}+v{VALUATION_VERSION}+core{core}"
    build = os.environ.get("DESK_BUNDLE_VERSION", "").strip()
    return f"{v}+build{build}" if build else v
