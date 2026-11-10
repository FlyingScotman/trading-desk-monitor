"""The runs affected by newly accepted private inputs, made again at once (TDM#547, TDM#548; 08 §2.2).

New inputs of business date D (trade terms, positions: stocks, in force until a newer set) change the inputs of the
default (EOD) run of D and of the current runs of today when today reads them. The autopilot's eod step notices the
former on its next cycle; its intraday and morning steps the latter. After a managed producer (the manual acquisition,
a services feed) the owner should not wait for either: ``after_inputs`` makes them now.

* ``eod_dates``: for each D the EOD run of D (``dayrun.eod_run``) when the cache holds its ``eod_final`` close, the store
  has positions for D and its default run is missing or read other inputs (position sets, private inputs hash).
* ``current_day``: today's shown runs (``store.CURRENT_RUNS``) whose private inputs hash is no longer today's (for an
  INTRADAY run with the day's trades applied, ``trades.intraday_inputs``, TDM#557): the
  latest INTRADAY run (an intraday cut, or the morning run on the previous close: ``cut_type`` ``prev_close``) while
  today has no EOD run, a preliminary close (``eod_preliminary`` EOD run) while today has no final one, each on the
  same market snapshot as before; the earlier INTRADAY runs of today go (``store.prune_intraday``, one per date as in
  R11). Today's final close is ``eod_dates``' (today in ``dates``) or the autopilot's.

The caller holds the cycle lock (``autopilot.CycleLock``). Never raises: a run that fails is listed in ``failed``
with its label (no exception text), the others still run. Returns ``{"runs": [{"run_id", "business_date", "label",
"previous_run_id"}], "failed": [{"business_date", "label", "code"}]}``.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path
from typing import Any

from . import store, trades
from .marketdata.bundle import SnapshotDir, SnapshotError

log = logging.getLogger("desk.recompute")


def _done(rid: int | None, d: date, label: str, previous: int | None, out: dict) -> None:
    if rid is not None:
        out["runs"].append({"run_id": rid, "business_date": d.isoformat(), "label": label, "previous_run_id": previous})


def _failed(d: date, label: str, e: Exception, out: dict) -> None:
    log.warning("recompute: %s failed (%s)", label, type(e).__name__)
    out["failed"].append({"business_date": d.isoformat(), "label": label,
                          "code": "market_missing" if isinstance(e, SnapshotError) else "run_failed"})


def eod_dates(settings: Any, url: str, dates: Iterable[date], *, out: Callable[[str], None] = lambda _: None,
              result: dict | None = None) -> dict:
    """The EOD runs of ``dates`` whose inputs changed (module doc)."""
    from .dayrun import eod_run

    res = result if result is not None else {"runs": [], "failed": []}
    sd = SnapshotDir(Path(settings.snapshot_cache))
    for d in sorted(set(dates)):
        try:
            sd.market_id(d.isoformat())
        except SnapshotError:
            continue  # no close of D in the cache: its EOD run comes with it (the autopilot's eod step)
        label = store.slice_label(d, "eod_final")
        try:
            with store.connect(url) as con:
                ids, n = store.current_sets(con, d)
                if not n:
                    continue
                run = store.default_runs(con, d).get(d)
                inputs_hash = trades.intraday_inputs(con, d, settings)[0].private_inputs_hash  # TDM#557
            if run is not None and run["position_set_ids"] == ids and run["private_inputs_hash"] == inputs_hash:
                continue
            rid = eod_run(settings, d.isoformat(), snapshots=settings.snapshot_cache, store_url=url,
                          report_dir=None, out=out)
        except Exception as e:  # noqa: BLE001 - one date's failure; the others still run
            _failed(d, label, e, res)
            continue
        _done(rid, d, label, run["run_id"] if run else None, res)
    return res


_LATEST_INTRADAY = ("SELECT run_id, cut_type, market_snapshot_id, cut_ts, lineage->'market_close', private_inputs_hash"
                    " FROM run r WHERE business_date = %s AND run_type = 'INTRADAY' AND status = 'complete'"
                    " AND NOT EXISTS (SELECT 1 FROM run e WHERE e.business_date = r.business_date"
                    " AND e.run_type = 'EOD' AND e.status = 'complete')"  # an EOD run of today is the one shown
                    " ORDER BY completed_at DESC, run_id DESC LIMIT 1")
_PRELIMINARY = ("SELECT run_id, market_snapshot_id, cut_ts, private_inputs_hash FROM run WHERE business_date = %s"
                " AND run_type = 'EOD' AND cut_type = 'eod_preliminary' AND status = 'complete'"
                " AND NOT EXISTS (SELECT 1 FROM run f WHERE f.business_date = %s AND f.run_type = 'EOD'"
                " AND f.cut_type = 'eod_final' AND f.status = 'complete')"
                " ORDER BY completed_at DESC, run_id DESC LIMIT 1")


def current_day(settings: Any, url: str, today: date, *, out: Callable[[str], None] = lambda _: None,
                result: dict | None = None) -> dict:
    """Today's current runs that read other private inputs than today's, made again (module doc)."""
    from .dayrun import eod_run

    res = result if result is not None else {"runs": [], "failed": []}
    try:
        with store.connect(url) as con:
            if not store.current_sets(con, today)[1]:
                return res
            want = want_intraday = trades.intraday_inputs(con, today, settings)[0].private_inputs_hash  # TDM#557
            intraday = con.execute(_LATEST_INTRADAY, (today,)).fetchone()
            prelim = con.execute(_PRELIMINARY, (today, today)).fetchone()
    except Exception as e:  # noqa: BLE001 - the store not readable: nothing made again, said so
        _failed(today, f"{today:%d.%m}", e, res)
        return res
    if intraday is not None and intraday[5] != want_intraday:
        rid0, cut, snap, cut_ts, close, _ = intraday
        label = store.slice_label(today, cut, cut_ts, "INTRADAY", close)
        try:
            if cut == "prev_close" and isinstance(close, dict):
                rid = eod_run(settings, today.isoformat(), snapshots=settings.snapshot_cache, store_url=url,
                              report_dir=None, out=out, run_type="INTRADAY",
                              on_close=(close["date"], close["cut_type"]), market_snapshot_id=snap)
            else:
                rid = eod_run(settings, today.isoformat(), snapshots=settings.snapshot_cache, store_url=url,
                              report_dir=None, out=out, cut_type=cut, market_snapshot_id=snap, run_type="INTRADAY")
            if rid is not None:
                with store.connect(url) as con:
                    store.prune_intraday(con, today, rid)
            _done(rid, today, label, rid0, res)
        except Exception as e:  # noqa: BLE001
            _failed(today, label, e, res)
    if prelim is not None and prelim[3] != want:
        rid0, snap, cut_ts, _ = prelim
        label = store.slice_label(today, "eod_preliminary", cut_ts)
        try:
            rid = eod_run(settings, today.isoformat(), snapshots=settings.snapshot_cache, store_url=url,
                          report_dir=None, out=out, cut_type="eod_preliminary", market_snapshot_id=snap,
                          run_type="EOD")
            _done(rid, today, label, rid0, res)
        except Exception as e:  # noqa: BLE001
            _failed(today, label, e, res)
    return res


def after_inputs(settings: Any, url: str, dates: Iterable[date], today: date, *,
                 out: Callable[[str], None] = lambda _: None) -> dict:
    """The EOD runs of ``dates`` (not after today), then today's current runs (module doc)."""
    days = sorted({d for d in dates if d <= today})
    res = eod_dates(settings, url, days, out=out)
    return current_day(settings, url, today, out=out, result=res) if days else res
