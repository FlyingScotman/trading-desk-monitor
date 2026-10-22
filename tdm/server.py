"""Веб-сервер: статика UI + API только для чтения. Стандартная библиотека, многопоточный."""
import json
import mimetypes
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .api import handle as api_handle
from .assemble import assemble, resolve_run
from .db import connect, init_db

STATIC_EXT = {".html", ".js", ".css", ".json", ".png", ".jpg", ".svg", ".ico", ".woff2"}  # .jpg: miniatures of the monitors
_cache, _cache_lock = {}, threading.Lock()  # (run_id, версия сценариев) -> JSON


def _desk_json(con, run):
    # прогон неизменяем, но к нему можно приложить сценарии, пересобрать исторические и догрузить котировки облигаций
    # (quote в позициях) — учитываем их в ключе
    # (1.3) — официальный PnL на T+1 (official_at), сценарии кэрри и график заседаний ЦБ
    ver = con.execute("SELECT (SELECT COUNT(*) FROM scenario_result WHERE run_id=?) || ':' ||"
                      " (SELECT COALESCE(MAX(updated_at), '') FROM scenario_def) || ':' ||"
                      " (SELECT COALESCE(MAX(updated_at), '') FROM md_bond_quote WHERE date <= ?) || ':' ||"
                      " (SELECT COALESCE(official_at, '') FROM calc_run WHERE run_id=?) || ':' ||"
                      " (SELECT COALESCE(MAX(updated_at), '') FROM carry_scenario_def) || ':' ||"
                      " (SELECT COALESCE(MAX(updated_at), '') FROM ref_cbr_meeting)",
                      (run["run_id"], run["as_of"], run["run_id"])).fetchone()[0]
    rid = (run["run_id"], ver)
    with _cache_lock:
        if rid in _cache:
            return _cache[rid]
    body = json.dumps(assemble(con, run), ensure_ascii=False, separators=(",", ":"))
    with _cache_lock:
        if len(_cache) > 16:
            _cache.pop(next(iter(_cache)))
        _cache[rid] = body
    return body


def make_handler(db_path, static_dir):
    static_dir = Path(static_dir).resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"tdm/{__version__}"

        def log_message(self, fmt, *args):  # короткий лог доступа
            print(f"{self.address_string()} {self.log_date_time_string()} {fmt % args}", flush=True)

        def _send(self, code, body, ctype, cache="no-store"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            path = unquote(u.path)
            try:
                if path == "/desk-data.js":
                    return self.desk_js(q)
                if path.startswith("/api/v1/"):
                    return self.api(path[len("/api/v1/"):], q)
                return self.static(path)
            except (ValueError, sqlite3.Error) as e:
                return self._json({"error": str(e)}, 400 if isinstance(e, ValueError) else 500)

        # ---- данные для страниц
        def desk_js(self, q):
            if q.get("demo"):
                return self._send(200, "/* demo requested: data.js */\n", "text/javascript; charset=utf-8")
            con = connect(db_path, readonly=True)
            try:
                run = resolve_run(con, q.get("as_of"), _int(q.get("run_id")))
                if not run:
                    msg = "нет опубликованных прогонов" + (f" на {q['as_of']}" if q.get("as_of") else "") if not q.get("run_id") else f"прогон {q['run_id']} не найден"
                    return self._send(200, f"/* {msg}: demo data.js */\nwindow.DESK_MISSING = {json.dumps(msg, ensure_ascii=False)};\n",
                                      "text/javascript; charset=utf-8")
                return self._send(200, "window.DESK = " + _desk_json(con, run) + ";\n", "text/javascript; charset=utf-8")
            finally:
                con.close()

        # ---- API
        def api(self, route, q):
            con = connect(db_path, readonly=True)
            try:
                res = api_handle(con, route, q)
                if res is not None:
                    return self._json(res, res.pop("_status", 200))
                if route == "health":
                    last = con.execute("SELECT run_id, as_of, run_type, published_at FROM calc_run WHERE status='complete'"
                                       " ORDER BY as_of DESC, published_at DESC LIMIT 1").fetchone()
                    return self._json({"status": "ok", "version": __version__, "db": str(db_path), "latest_run": dict(last) if last else None})
                if route == "runs":
                    lim = min(_int(q.get("limit")) or 50, 1000)
                    sql, args = "SELECT run_id, as_of, run_type, status, published_at, started_at, source, lib_version, error FROM calc_run", []
                    where = []
                    if q.get("as_of"):
                        where.append("as_of=?"); args.append(q["as_of"])
                    if q.get("status", "complete") != "all":
                        where.append("status=?"); args.append(q.get("status", "complete"))
                    if where:
                        sql += " WHERE " + " AND ".join(where)
                    rows = con.execute(sql + " ORDER BY as_of DESC, run_id DESC LIMIT ?", args + [lim]).fetchall()
                    return self._json({"runs": [dict(r) for r in rows]})
                if route.startswith("runs/"):
                    rid = _int(route[5:])
                    run = con.execute("SELECT * FROM calc_run WHERE run_id=?", (rid,)).fetchone()
                    if not run:
                        return self._json({"error": "not found"}, 404)
                    log = con.execute("SELECT ts, level, message FROM calc_run_log WHERE run_id=? ORDER BY rowid", (rid,)).fetchall()
                    cnt = {t: con.execute(f"SELECT COUNT(*) FROM {t} WHERE run_id=?", (rid,)).fetchone()[0]
                           for t in ("position", "position_risk", "position_pnl", "funding_deal", "funding_rule", "run_ocp_transfer", "scenario_result")}
                    return self._json({"run": dict(run), "counts": cnt, "log": [dict(r) for r in log]})
                if route == "desk":
                    run = resolve_run(con, q.get("as_of"), _int(q.get("run_id")))
                    if not run:
                        return self._json({"error": "нет опубликованного прогона"}, 404)
                    return self._send(200, _desk_json(con, run), "application/json; charset=utf-8")
                return self._json({"error": "unknown endpoint"}, 404)
            finally:
                con.close()

        # ---- статика
        def static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            f = (static_dir / path.lstrip("/")).resolve()
            if static_dir not in f.parents or f.suffix not in STATIC_EXT or not f.is_file():
                return self._send(404, "not found", "text/plain; charset=utf-8")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            return self._send(200, f.read_bytes(), ctype, cache="no-cache")

    return Handler


def _int(v):
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        raise ValueError(f"ожидалось целое число: {v!r}")


def serve(db_path, host="0.0.0.0", port=8080, static_dir=None):
    init_db(db_path).close()
    static_dir = static_dir or Path(__file__).resolve().parent.parent / "prototypes"
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path, static_dir))
    httpd.daemon_threads = True
    print(f"Trading Desk Monitor {__version__}: http://{'localhost' if host in ('0.0.0.0', '') else host}:{port}/  (база: {db_path}, статика: {static_dir})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
