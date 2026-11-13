"""Diagnostics route (R13) behind the settings page's «Диагностика»: ``desk.diagnostics.build`` as JSON, for the
preview, or as a file to save (``download=1``: ``desk-diagnostics-<UTC>.json``). Local only, like the settings
routes: the document has no private data, but it does describe this machine's setup.

``GET /api/v1/diagnostics/slow-requests`` (TDM#727) is the settings page's «Медленные запросы»: the last slow requests
of this server run (``desk.api.slowlog``), the same list as the file's ``slow_requests``.

``GET /api/v1/backfill/status`` (R23) is the settings page's «История прогонов» and the header's indicator:
``desk.backfill.status`` (dates and counts, the store and the cache only, no hub call; ``todo_by_reason``) and
``hub_problem_dates``, the autopilot sync step's ``problem_dates`` (#163); ``step`` the backfill step of this cycle, else
the last finished one (``last_step``), ``current`` the date being computed now with the cumulative count (TDM#587).

``GET /api/v1/inbox/reconcile`` (R22) is the settings page's «Загрузка позиций»: ``desk inbox reconcile`` as JSON,
book ids and file names included (the owner's own machine, local only). ``GET /api/v1/inbox/folders`` (TDM#445) is
its «Папки с отчётами»: per folder what the inbox found there (``desk.daystatus.folders``), the folders' paths
included, so local only too.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .. import diagnostics, store
from ..settings import SettingsError, load
from .settings_routes import local_only

router = APIRouter(dependencies=[Depends(local_only)])


def slow_of(request: Request) -> dict | None:
    slow = getattr(request.app.state, "slow", None)
    return slow.snapshot() if slow is not None else None


@router.get("/api/v1/diagnostics/slow-requests")
def get_slow_requests(request: Request) -> Response:
    """The settings page's «Медленные запросы» (TDM#727): the last requests this server run answered slower than
    ``threshold_s`` (``DESK_SLOW_REQUEST_S``, 2 s), the newest first: route as declared (no ids), query parameter names
    (no values), status, ms, ``cache`` (``hit``, ``miss``: a body was built for it; null: no such cache), bytes."""
    import json

    return Response(json.dumps(slow_of(request) or {"threshold_s": None, "total": 0, "keep": 0, "requests": []},
                               ensure_ascii=False), media_type="application/json; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@router.get("/api/v1/diagnostics")
def get_diagnostics(request: Request, hub: bool = True, download: bool = False) -> Response:
    """``hub=0`` does not ask the hub (no live check, no list of its snapshots)."""
    now = datetime.now(UTC)
    doc = diagnostics.build(store_url=request.app.state.store_url, autopilot=getattr(request.app.state, "autopilot",
                                                                                     None),
                            hub=hub, started_at=getattr(request.app.state, "started_at", None), now=now,
                            lan_probe=getattr(request.app.state, "lan_probe", None),
                            slow_requests=slow_of(request))
    headers = {"Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{diagnostics.file_name(now)}"'
    return Response(diagnostics.dumps(doc), media_type="application/json; charset=utf-8", headers=headers)


@router.get("/api/v1/lan/status")
def get_lan_status(request: Request, probe: bool = False) -> Response:
    """The settings page's «Сеть» (``desk lan status``): address, HTTPS and its certificate's names, the firewall
    rule, the URLs for colleagues, the self-check made at start (``last``) and, with ``probe=1``, a fresh one."""
    import json

    from .. import lan

    try:
        settings = load()
    except SettingsError as e:
        raise HTTPException(500, f"settings: {e}") from None
    listen = getattr(request.app.state, "listen", None) or {}
    doc = lan.status(settings, host=listen.get("host"), port=listen.get("port"), tls=listen.get("tls"), run_probe=probe,
                     instance=getattr(request.app.state, "instance", None))
    doc["last"] = getattr(request.app.state, "lan_probe", None)
    return Response(json.dumps(doc, ensure_ascii=False, default=str), media_type="application/json; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@router.get("/api/v1/inbox/reconcile")
def get_reconcile(request: Request, on: str | None = None) -> Response:
    """Per book the rows of the report, accepted, in the default run, and the losses (``desk inbox reconcile``);
    ``?on=YYYY-MM-DD`` for another business date."""
    import json

    from ..inbox import scan

    try:
        day = date.fromisoformat(on) if on else None
    except ValueError:
        raise HTTPException(400, "on must be YYYY-MM-DD") from None
    try:
        settings = load()
    except SettingsError as e:
        raise HTTPException(500, f"settings: {e}") from None
    with store.connect(request.app.state.store_url or store.resolve_url(settings)) as con:
        doc = scan.reconcile(con, settings, day)
    return Response(json.dumps(doc, ensure_ascii=False, default=str), media_type="application/json; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@router.get("/api/v1/inbox/folders")
def get_inbox_folders(request: Request) -> Response:
    """Per scanned folder what the inbox knows of its files: by source and status, the files never read, the latest
    business date read per dataset (``daystatus.folders``, TDM#445)."""
    import json

    from .. import daystatus

    try:
        settings = load()
    except SettingsError as e:
        raise HTTPException(500, f"settings: {e}") from None
    with store.connect(request.app.state.store_url or store.resolve_url(settings)) as con:
        doc = daystatus.folders(con, settings)
    return Response(json.dumps(doc, ensure_ascii=False, default=str), media_type="application/json; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@router.get("/api/v1/backfill/status")
def get_backfill_status(request: Request) -> Response:
    """The history of runs: the range, dates with their run, to run, without positions, failed; the oldest run."""
    import json

    from .. import backfill

    try:
        settings = load()
    except SettingsError as e:
        raise HTTPException(500, f"settings: {e}") from None
    with store.connect(request.app.state.store_url or store.resolve_url(settings)) as con:
        doc = backfill.status(con, settings)
    ap = getattr(request.app.state, "autopilot", None)
    st = ap.status() if ap is not None else {}
    steps = st.get("steps", [])
    if ap is not None:
        # TDM#587: this cycle's step, else the last finished one (steps are cleared at each cycle's start)
        last = ap.last_backfill() if hasattr(ap, "last_backfill") else None
        doc["step"] = next((x for x in steps if x["name"] == "backfill"), None) or last
        doc["last_step"] = last
        pr = st.get("progress") or {}
        # the date being computed now and the cumulative count (``backfill.run``'s progress)
        doc["current"] = ({"date": pr.get("detail"), "done": pr.get("done"), "total": pr.get("total")}
                          if pr.get("step") == "backfill" and len(str(pr.get("detail") or "")) == 10 else None)
    # the hub dates the sync step cannot load, waiting for a retry (#146): the header counts them among the dates
    # that do not load (#163, #447)
    doc["hub_problem_dates"] = next((x.get("problem_dates") or [] for x in steps if x["name"] == "sync"), [])
    return Response(json.dumps(doc, ensure_ascii=False, default=str), media_type="application/json; charset=utf-8",
                    headers={"Cache-Control": "no-store"})
