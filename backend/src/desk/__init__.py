"""Trading Desk Monitor backend (system docs: docs/architecture/04_BACKEND.md)."""

from __future__ import annotations

import os

__version__ = "0.2.12"

# Bumped by hand when a change makes a run compute something else from the same inputs (valuation, PnL, risk
# buckets, coverage). A run records ``logic_version()`` in its lineage; the autopilot recomputes the latest positions
# date once when its default run was made by another logic version; the backfill (desk.backfill, R23) recomputes the
# older dates in the background.
VALUATION_VERSION = 28  # 2: risk per curve (R8); 3: static display fields (R9 D1-D6, D8: names, coupons, issuers);
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
# 15: KBD floaters (ZR_YLD_CRV, contract 1.4 terms) through core (#33): coupons projected on the KBD (RUB OFZ, their
# OFZ basis risk, extra.ofz_basis01), discounted on RUONIA OIS + Z, their DM to the KBD
# 16: a foreign bond's FX delta net of its currency funding liability (clean MV + the accrued paid at the purchase,
# OQ-04, TDM#54, TDM#169; publish.fx_liability), its FX revaluation in the PnL's fx_funding; bond currency fallback
# 17: core 57fb6b7 (core#90): a KBD floater in RUONIA + basis, its RUONIA risk computed (extra.ofz_ruonia01), not variant P
# 18: a foreign bond's OCP liability does not follow its MV (OQ-17, TDM#266): the D-1 dirty MV, so its FX delta is the
# day's FX PnL (dirty MV change + coupons); fx_funding revalues the D-1 dirty MV
# 19: a KBD floater's margin per period by the hub's spread_schedule (a step at a put or a call offer, TDM#252)
# 20: no call is the horizon (TDM#35): a perpetual never called (core never_called, its tail in the flash PnL and the
# fallback; core 0.41: floating and KBD-reset ones too, TDM#383), a callable to maturity; a «known coupons» bond with a
# key-rate/RUONIA formula valued as a floater (TDM#401)
# 21: the flash PnL keeps its breakdowns in pnl.extra (TDM#406): carry_coupon / carry_pull, rates_linear_curves,
# curve_moves_bp, prev; the owner chose to recompute the history so old dates get them (TDM#417)
# 22: a RUB bond without a price of its own today (an intraday cut before its first trade, a day it did not trade) is
# valued and carried by core at the ladder's price (fallback.at_last_price, TDM#383): a floater's projection, OFZ basis
# and tail instead of a duration estimate, core's accrued and value for every one (the forward carry's base); a
# report-valued row whose D-1 carried an older report names that report's date (extra.report_from, TDM#440)
# 23: a model price is valued and carried by core at that price (fallback.at_model_price, TDM#441); a price rung core
# valued on both dates takes the exact flash steps (a model <-> market switch: its spread step in other, flagged); every
# PnL with an unexplained part records why in pnl.extra.other_reason (pnl.reasons, TDM#442); a bond's extra.cashflows
# go to maturity, past the 18 months and its offer (extra.cashflows_to, TDM#166)
# 24: a position with an impossible risk (extra.risk_problem, TDM#170) is in no book's DV01 or CS01 of the run's totals
# (TDM#460); the history recomputed gets the flag, so the pages leave its risk out on every date; a RUB bond on the KBD
# for want of the RUONIA curve on its date is marked (extra.curve_proxy, TDM#31; unreleased 24, so the recomputed
# history gets it too)
# 25: a foreign bond's linear rates (rates_linear, rates_linear_curves) in RUB at the D rate, as rates (TDM#418)
# 26: core 0.42 (TDM#499): RUONIA OIS past its last quote on synthetic 15Y/20Y quotes (OisTail), core 0.41.1 accrued
# 28: a KBD floater's linear rates PnL takes its RUONIA discounting term too: rates_linear, its RUONIA/basis split and convexity (TDM#467)


def logic_version() -> str:
    """``<backend version>+v<VALUATION_VERSION>+core<core version>``, and ``+build<bundle VERSION>`` when run from the
    Windows bundle (``DESK_BUNDLE_VERSION``, set by deskctl): the program that made a run. Whether two values compute
    the same numbers is ``backfill.same_logic``: only ``VALUATION_VERSION`` and the core version count, so a release
    without changes to the computation does not recompute the history (issue #24)."""
    from rub_fixed_income import __version__ as core

    v = f"{__version__}+v{VALUATION_VERSION}+core{core}"
    build = os.environ.get("DESK_BUNDLE_VERSION", "").strip()
    return f"{v}+build{build}" if build else v
