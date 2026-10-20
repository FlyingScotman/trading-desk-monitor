"""Diagnostics route (R13) behind the settings page's «Диагностика»: ``desk.diagnostics.build`` as JSON, for the
preview, or as a file to save (``download=1``: ``desk-diagnostics-<UTC>.json``). Local only, like the settings
routes: the document has no private data, but it does describe this machine's setup.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from .. import diagnostics
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
