"""Diagnostics route (R13) behind the settings page's «Диагностика»: ``desk.diagnostics.build`` as JSON, for the
preview, or as a file to save (``download=1``: ``desk-diagnostics-<UTC>.json``). Local only, like the settings
routes: the document has no private data, but it does describe this machine's setup.

``GET /api/v1/inbox/reconcile`` (R22) is the settings page's «Загрузка позиций»: ``desk inbox reconcile`` as JSON,
book ids and file names included (the owner's own machine, local only).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .. import diagnostics, store
from ..settings import SettingsError, load
from .settings_routes import local_only

router = APIRouter(dependencies=[Depends(local_only)])


@router.get("/api/v1/diagnostics")
def get_diagnostics(request: Request, hub: bool = True, download: bool = False) -> Response:
    """``hub=0`` does not ask the hub (no live check, no list of its snapshots)."""
    now = datetime.now(UTC)
    doc = diagnostics.build(store_url=request.app.state.store_url, autopilot=getattr(request.app.state, "autopilot",
                                                                                     None),
                            hub=hub, started_at=getattr(request.app.state, "started_at", None), now=now)
    headers = {"Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{diagnostics.file_name(now)}"'
    return Response(diagnostics.dumps(doc), media_type="application/json; charset=utf-8", headers=headers)


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
