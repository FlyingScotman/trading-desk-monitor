"""Start (or reuse) the local development PostgreSQL of the desk and print its URL.

    python tools/devdata/devpg.py PGDATA [DBNAME]

Uses pgserver (a PostgreSQL 16 build on PyPI), so no system PostgreSQL is needed. The server
listens on a Unix socket inside PGDATA only and keeps running after this script exits
(``cleanup_mode=None``). Creates DBNAME (UTF8, from template0) when it does not exist.
Run with the interpreter of the dev-data venv (see build_dev_snapshots.sh).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pgserver


def main() -> None:
    pgdata = Path(sys.argv[1]).resolve()
    dbname = sys.argv[2] if len(sys.argv) > 2 else None
    pgdata.parent.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(pgdata, cleanup_mode=None)
    if dbname:
        exists = server.psql(f"SELECT 1 FROM pg_database WHERE datname = '{dbname}';")
        if "1 row" not in exists:
            server.psql(
                f"CREATE DATABASE {dbname} ENCODING 'UTF8' TEMPLATE template0 "
                "LC_COLLATE 'C' LC_CTYPE 'C';"
            )
        print(server.get_uri(dbname))
    else:
        print(server.get_uri())


if __name__ == "__main__":
    main()
