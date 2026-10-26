"""The legacy ``tdm`` SQLite writer of a run, for the compat parity test only (``test_api``).

The backend no longer writes the tdm store (#294); ``api.compat`` still promises ``tdm.assemble``'s shape until it is
removed (#107, step 4), and the parity test checks that promise against a tdm store this helper fills the way
``desk eod --tdm`` did. It goes with compat and the ``tdm`` package.
"""

from __future__ import annotations

import json

from rub_fixed_income import __version__ as CORE_VERSION
from tdm.writer import Run

from desk import __version__ as BACKEND_VERSION
from desk.publish.tdm_run import REFERENCE_KINDS, run_content

TDM_INSTR_TYPES = ("BOND", "IRS", "XCCY", "CDS", "FX")


def publish(db: str, ctx, positions, *, prev) -> tuple[int, str, list[str]]:
    lineage = {**ctx.lineage(), "backend_version": BACKEND_VERSION, "core_version": CORE_VERSION,
               "prev_market_snapshot_id": prev.snapshot.id if prev else None}
    c = run_content(ctx, positions, prev, lineage)
    # the tdm store knows the coupon types fix and float only (a CHECK) and the instrument types of TDM_INSTR_TYPES
    rows = [{**r, "coupon_type": "float" if r["coupon_type"] == "float" else "fix"} if r.get("coupon_type") else r
            for r in c.rows if r["instr_type"] in TDM_INSTR_TYPES]
    with Run(db, as_of=ctx.business_date.isoformat(), run_type="EOD",
             source=f"desk-backend {BACKEND_VERSION} (slice {'S2' if any(o.pnl for _, _, o in positions) else 'S1'})",
             lib_version=f"rub-fixed-income-core {CORE_VERSION}",
             market_snapshot=ctx.market.snapshot.id, comment=json.dumps(lineage, ensure_ascii=False),
             prev_date=prev.business_date.isoformat() if prev else None, jtd_recovery=0.0) as run:
        for kind in REFERENCE_KINDS:
            getattr(run, f"add_{kind}")(c.reference[kind])
        run.add_positions(rows)
        for level, message in c.log:
            run.log(level, message)
    return run.run_id, run.status, run.warnings
