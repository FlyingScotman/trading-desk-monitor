#!/usr/bin/env bash
# Build real Market Snapshot Contract v1 bundles for local development of the desk backend.
#
#   tools/devdata/build_dev_snapshots.sh [DATE ...]      # default: the last five seed days
#
# Restores the hub's seed dump into a separate local PostgreSQL (.devdata/pg, pgserver; the
# hub's own seed database is not touched), applies the hub's migrations with the hub code from
# MDHUB_SRC, publishes eod_final snapshots for the dates with the hub's snapshot builder and
# exports the bundles to .devdata/snapshots/. Steps already done are skipped on a re-run.
#
# MDHUB_SRC     hub checkout with the snapshot layer (default: the snapshot-layer worktree)
# MDHUB_BUNDLE  hub release bundle with db/mdhub.dump (default: dist/ of the hub checkout)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HUB="$(cd "$ROOT/../market-data-server" && pwd)"
MDHUB_SRC="${MDHUB_SRC:-$HUB/.claude/worktrees/silly-tharp-ed59c7}"
MDHUB_BUNDLE="${MDHUB_BUNDLE:-$HUB/dist/market-data-hub-20261016.tar.gz}"
DEV="$ROOT/.devdata"
VENV="$DEV/venv-mdhub"
PY="$VENV/bin/python"
DATES=("$@")
[[ ${#DATES[@]} -gt 0 ]] || DATES=(2026-10-09 2026-10-12 2026-10-13 2026-10-14 2026-10-15)

log() { echo "==> $*"; }
mkdir -p "$DEV"

if [[ ! -f "$DEV/mdhub/db/mdhub.dump" ]]; then
    log "extracting the seed dump from $(basename "$MDHUB_BUNDLE")"
    tar -xzf "$MDHUB_BUNDLE" -C "$DEV" mdhub/db/mdhub.dump mdhub/db/mdhub.dump.sha256
fi

if [[ ! -x "$PY" ]]; then
    log "creating $VENV with the hub from $MDHUB_SRC"
    uv venv --python 3.12 "$VENV"
    uv pip install --python "$PY" "$MDHUB_SRC" "pgserver>=0.1.4"
fi

URL="$("$PY" "$ROOT/tools/devdata/devpg.py" "$DEV/pg" mdhub)"
PG_BIN="$("$PY" -c 'import pgserver, pathlib; print(pathlib.Path(pgserver.__file__).parent / "pginstall" / "bin")')"
log "dev database: $URL"

restored="$("$PG_BIN/psql" -X -A -t -d "$URL" -c "SELECT to_regclass('ops.schema_migration') IS NOT NULL")"
if [[ "$restored" != "t" ]]; then
    log "restoring the seed (a few minutes)"
    PG_BIN="$PG_BIN" MDHUB_CMD="$VENV/bin/mdhub" \
        bash "$MDHUB_SRC/deploy/restore_seed.sh" --database-url "$URL" --yes "$DEV/mdhub/db/mdhub.dump"
else
    log "seed already restored; applying pending migrations"
    MDHUB_DATABASE_URL="$URL" "$VENV/bin/mdhub" migrate
fi

log "publishing and exporting snapshots: ${DATES[*]}"
MDHUB_DATABASE_URL="$URL" "$PY" "$ROOT/tools/devdata/export_snapshots.py" "$DEV/snapshots" "${DATES[@]}"
