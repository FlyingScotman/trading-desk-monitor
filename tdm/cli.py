"""Командная строка: python -m tdm <команда>."""
import argparse
import json
import sys
from pathlib import Path

from .db import DEFAULT_DB, connect, init_db

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m tdm", description="Trading Desk Monitor")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"файл базы SQLite (по умолчанию {DEFAULT_DB}, или переменная TDM_DB)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="запустить веб-сервер")
    s.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = доступен с других машин; 127.0.0.1 = только локально")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--static", default=None, help="папка со страницами (по умолчанию prototypes/)")
    sub.add_parser("init-db", help="создать пустую базу")
    i = sub.add_parser("import", help="загрузить JSON-пакет прогона")
    i.add_argument("bundle", nargs="+")
    sub.add_parser("demo", help="загрузить демо-прогон (fixtures/demo_bundle.json)")
    r = sub.add_parser("runs", help="список прогонов")
    r.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("purge", help="удалить старые прогоны")
    p.add_argument("--keep-days", type=int, default=90, help="хранить все прогоны за последние N дат")
    p.add_argument("--keep-eod", action="store_true", help="у более старых дат оставлять последний EOD (по умолчанию удаляются)")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        from .server import serve
        return serve(a.db, a.host, a.port, a.static)
    if a.cmd == "init-db":
        init_db(a.db).close()
        print("база готова:", a.db)
    elif a.cmd in ("import", "demo"):
        from .importer import import_bundle
        files = a.bundle if a.cmd == "import" else [str(ROOT / "fixtures" / "demo_bundle.json")]
        rc = 0
        for f in files:
            try:
                rid, status, warns = import_bundle(a.db, f)
                print(f"{f}: прогон {rid} — {status}" + (f", предупреждений: {len(warns)}" if warns else ""))
                for w in warns[:20]:
                    print("  WARN", w)
            except Exception as e:  # noqa: BLE001
                print(f"{f}: ОШИБКА — {e}", file=sys.stderr)
                rc = 1
        sys.exit(rc)
    elif a.cmd == "runs":
        con = init_db(a.db)
        for r in con.execute("SELECT run_id, as_of, run_type, status, published_at, error FROM calc_run ORDER BY run_id DESC LIMIT ?", (a.limit,)):
            print(f"{r['run_id']:>6}  {r['as_of']}  {r['run_type']:<8} {r['status']:<9} {r['published_at'] or '':<19} {r['error'] or ''}")
    elif a.cmd == "purge":
        con = init_db(a.db)
        dates = [r[0] for r in con.execute("SELECT DISTINCT as_of FROM calc_run ORDER BY as_of DESC")]
        old = dates[a.keep_days:]
        with con:
            n = 0
            for d in old:
                keep = None
                if a.keep_eod:
                    k = con.execute("SELECT run_id FROM calc_run WHERE as_of=? AND run_type='EOD' AND status='complete' ORDER BY run_id DESC LIMIT 1", (d,)).fetchone()
                    keep = k[0] if k else None
                n += con.execute("DELETE FROM calc_run WHERE as_of=? AND run_id IS NOT ?", (d, keep)).rowcount
            n += con.execute("DELETE FROM calc_run WHERE status='failed' AND as_of NOT IN (SELECT as_of FROM calc_run ORDER BY as_of DESC LIMIT 1)").rowcount
        con.execute("VACUUM")
        print(f"удалено прогонов: {n}; история итогов (desk_history) сохранена")
