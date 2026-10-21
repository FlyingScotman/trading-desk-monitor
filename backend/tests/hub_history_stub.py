"""A stub of the hub's history API (hub 0.2.0, ``/v1/history/*``; its docs/HISTORY_API.md) on 127.0.0.1, with
synthetic history built from the golden fixture market of 2026-10-15: every weekday back to ``start`` (bonds:
2024-01-03, the hub's first EOD) the fixture's bonds with drifting prices and yields, the KBD, RUONIA OIS and
key-rate swap quote sets of two sources, fixings (KEY_RATE, RUONIA, RUSFAR, RUSFARCNY, RUSFARIND as an index,
CPI_RU_MOM) and FX (cbr_official every calendar day, moex_eod CNY/RUB TOM). Not a test module: the history tests and
``concepts_live.py`` use it.

As the hub: the columnar envelope, key columns always, ``fields``, keyset paging with an opaque cursor bound to the
query (another query's cursor: 400), ``limit`` up to 50 000, a weak ETag of the query, the page and the selection's
version (its row count and latest write) with ``If-None-Match`` -> 304, gzip from 1 KB with ``Accept-Encoding``,
``data_version`` = the latest write of the whole selection; 401 without the token, 403 for a token without
``read`` (``mode = "no_read"``), 404 on everything for a hub before 0.2.0 (``mode = "no_api"``)."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import threading
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
TOKEN = "mdh_history_token_0123456789"
END = date(2026, 10, 15)

BOND_COLUMNS = ["trade_date", "secid", "board", "open_pct", "low_pct", "high_pct", "close_pct", "legal_close_pct",
                "waprice_pct", "market_price2_pct", "market_price3_pct", "admitted_quote_pct", "accrued_per_bond",
                "face_value", "face_unit", "currency_id", "volume_bonds", "value_ccy", "num_trades",
                "trading_session", "yield_close_pct", "yield_at_waprice_pct", "effective_yield_pct",
                "effective_yield_waprice_pct", "yield_to_offer_pct", "yield_date", "yield_date_type", "duration_days",
                "duration_waprice_days", "zspread_bp", "gspread_bp", "yield_last_coupon_pct", "icpi", "ir", "bei",
                "cbr"]
ZCYC_COLUMNS = ["trade_date", "trade_time", "source_ts", "is_eod", "labels", "B1", "B2", "B3", "T1",
                *(f"G{i}" for i in range(1, 10)), "standard_yields", "check_max_error_bp", "check_tolerance_bp",
                "hub_ingested_at"]
CURVE_COLUMNS = ["snapshot_date", "source", "quote_set_id", "fetched_at", "source_ts", "quote_basis", "market_closed",
                 "label", "content_sha256", "labels", "meta", "hub_ingested_at", "tenor", "position", "tenor_months",
                 "tenor_days", "pillar_date", "bid_pct", "mid_pct", "ask_pct", "extra"]
FIXING_COLUMNS = ["index_code", "source", "fixing_date", "value", "unit", "effective_date", "published_on",
                  "published_at", "hub_ingested_at", "origin", "labels", "details"]
FX_COLUMNS = ["kind", "pair", "base_ccy", "quote_ccy", "date", "rate", "secid", "board", "tenor", "close", "waprice",
              "quote_units", "close_per_unit", "waprice_per_unit", "num_trades", "nominal", "set_date", "trade_time",
              "hub_ingested_at"]
DATASETS = {  # name -> (columns, key columns (the order), date column)
    "bonds": (BOND_COLUMNS, ("secid", "trade_date", "board"), "trade_date"),
    "zcyc": (ZCYC_COLUMNS, ("trade_date", "trade_time"), "trade_date"),
    "curves": (CURVE_COLUMNS, ("snapshot_date", "source", "quote_set_id", "position", "tenor"), "snapshot_date"),
    "fixings": (FIXING_COLUMNS, ("index_code", "source", "fixing_date"), "fixing_date"),
    "fx": (FX_COLUMNS, ("kind", "pair", "date", "secid", "board"), "date"),
}
KEYS = {"bonds": ("trade_date", "secid", "board"), "zcyc": ("trade_date", "trade_time"),
        "curves": ("snapshot_date", "source", "quote_set_id", "position", "tenor"),
        "fixings": ("index_code", "source", "fixing_date"), "fx": ("kind", "pair", "date", "secid", "board")}
WRITTEN = "2026-10-16T05:00:00+00:00"


def weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _r(v: float, n: int) -> float:
    return round(v, n)


def market_doc() -> dict:
    index = json.loads((FIX / "golden_index.json").read_text())["fixtures"]
    with gzip.open(FIX / f"{index['market_eod_final']['snapshot_id']}.json.gz") as f:
        return json.load(f)


def synthetic(end: date = END, bonds_from: date = date(2024, 1, 3), rates_from: date = date(2023, 1, 2),
              written: str = WRITTEN) -> dict[str, list[dict[str, Any]]]:
    """The datasets' rows (dicts with the hub's column names, plus ``_w``: when the hub wrote the row)."""
    doc = market_doc()
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in DATASETS}
    days = weekdays(bonds_from, end)
    n = len(days)
    quotes = {}
    for q in doc["data"]["bond_quotes"]:  # the board with a legal close, as the hub's primary board
        if q.get("legal_close_pct") is not None and (q["secid"] not in quotes or q["board"] != "TQOD"):
            quotes.setdefault(q["secid"], q)
    for secid, q in sorted(quotes.items()):
        sa = q.get("source_analytics") or {}
        h = int(hashlib.sha256(secid.encode()).hexdigest()[:6], 16)
        for i, d in enumerate(days):
            back = n - 1 - i  # business days before the fixture date
            wave = math.sin((back + h % 50) / 23.0)
            px = float(q["legal_close_pct"]) - back * 0.004 + wave * 0.6
            y = float(sa["effective_yield_pct"]) + back * 0.0008 - wave * 0.15 if sa.get("effective_yield_pct") else None
            linker = secid.startswith("SU52")
            out["bonds"].append({
                "trade_date": d.isoformat(), "secid": secid, "board": q["board"], "close_pct": _r(px - 0.05, 4),
                "legal_close_pct": _r(px, 4), "waprice_pct": _r(px - 0.02, 4), "accrued_per_bond": 1.25,
                "face_value": 1000, "face_unit": q.get("face_unit"), "currency_id": q.get("currency_id"),
                "volume_bonds": 1000 + h % 977, "value_ccy": _r((1000 + h % 977) * px * 10, 2), "num_trades": 10 + h % 90,
                "effective_yield_pct": y and _r(y, 4), "yield_close_pct": y and _r(y + 0.1, 4),
                "yield_to_offer_pct": y and _r(y, 4), "yield_date": sa.get("yield_date"),
                "yield_date_type": sa.get("yield_date_type"), "duration_days": sa.get("duration_days") and 512,
                "zspread_bp": None if y is None else 150 + h % 100 + round(wave * 10),
                "gspread_bp": None if y is None else 160 + h % 100 + round(wave * 10),
                "icpi": _r(1.25 + back * -0.0003, 4) if linker else None, "ir": _r(6.2 + wave * 0.2, 2) if linker else None,
                "bei": _r(9.4 - wave * 0.3, 2) if linker else None, "cbr": None, "_w": written})
    z = doc["data"]["zcyc_params"][0]
    rdays = weekdays(rates_from, end)
    rn = len(rdays)
    for i, d in enumerate(rdays):
        back = rn - 1 - i
        p = {k: float(v) for k, v in z["params"].items()}
        p["B1"] += -back * 0.6 + math.sin(back / 30) * 40
        out["zcyc"].append({"trade_date": d.isoformat(), "trade_time": "18:54:59", "is_eod": True, "labels": [],
                            **{k: _r(v, 6) for k, v in p.items()}, "standard_yields": [], "_w": written})
        for ds, base in (("ruonia_ois", 15.0), ("key_rate_swap", 15.4)):
            for src, qid, two in (("otc_monitor", 1, True), ("ratescalcs", 2, False)):
                for pos, (ten, months) in enumerate((("3M", 3), ("6M", 6), ("1Y", 12), ("2Y", 24), ("3Y", 36),
                                                     ("5Y", 60), ("7Y", 84), ("10Y", 120))):
                    mid = base + back * 0.002 - months / 120 * 1.5 + math.sin(back / 40) * 0.4 + (0.05 if two else 0)
                    out["curves"].append({
                        "_ds": ds, "snapshot_date": d.isoformat(), "source": src, "quote_set_id": qid * 100000 + i,
                        "fetched_at": f"{d.isoformat()}T15:00:00+00:00", "quote_basis": "bid_ask_mid" if two else
                        "mid_only", "labels": [] if two else ["mid_only"], "tenor": ten, "position": pos,
                        "tenor_months": months, "tenor_days": None, "bid_pct": _r(mid - 0.05, 4) if two else None,
                        "mid_pct": _r(mid, 4), "ask_pct": _r(mid + 0.05, 4) if two else None, "_w": written})
        key = (21.0 if d < date(2025, 6, 9) else 20.0 if d < date(2025, 7, 28) else 18.0 if d < date(2025, 9, 15)
               else 17.0 if d < date(2026, 7, 27) else 16.5)
        for code, v, unit in (("KEY_RATE", key, "pct"), ("RUONIA", key - 0.3 + math.sin(back / 9) * 0.1, "pct"),
                              ("RUSFAR", key - 0.4 + math.sin(back / 7) * 0.1, "pct"),
                              ("RUSFARCNY", 7.5 + math.sin(back / 11) * 0.5, "pct"),
                              ("RUSFARIND", 2000 + i * 0.9, "index")):
            out["fixings"].append({"index_code": code, "source": "cbr" if code in ("KEY_RATE", "RUONIA") else
                                   "moex_iss", "fixing_date": d.isoformat(), "value": _r(v, 4), "unit": unit,
                                   "effective_date": d.isoformat() if code == "KEY_RATE" else None,
                                   "published_on": None, "published_at": None,
                                   "hub_ingested_at": f"{d.isoformat()}T16:00:00+00:00", "origin": "md.fixing",
                                   "labels": [], "details": None, "_w": written})
        out["fx"].append({"kind": "moex_eod", "pair": "CNY/RUB", "base_ccy": "CNY", "quote_ccy": "RUB",
                          "date": d.isoformat(), "rate": None, "secid": "CNYRUB_TOM", "board": "CETS", "tenor": "TOM",
                          "close": _r(11.3 + math.sin(back / 15) * 0.3, 4), "waprice": None, "quote_units": 1,
                          "close_per_unit": _r(11.3 + math.sin(back / 15) * 0.3, 4), "waprice_per_unit": None,
                          "num_trades": 800, "nominal": None, "_w": written})
    m = date(rates_from.year, rates_from.month, 1)
    k = 0
    while m <= end:
        out["fixings"].append({"index_code": "CPI_RU_MOM", "source": "rosstat", "fixing_date": m.isoformat(),
                               "value": _r(100.4 + math.sin(k / 3) * 0.3, 2), "unit": "index", "effective_date": None,
                               "published_on": None, "published_at": None, "hub_ingested_at": None,
                               "origin": "md.fixing", "labels": [], "details": None, "_w": written})
        m, k = (m + timedelta(days=32)).replace(day=1), k + 1
    d = rates_from
    while d <= end:
        back = (end - d).days
        for c, base in (("USD", 81.0), ("EUR", 94.0), ("CNY", 11.3)):
            out["fx"].append({"kind": "cbr_official", "pair": f"{c}/RUB", "base_ccy": c, "quote_ccy": "RUB",
                              "date": d.isoformat(), "rate": _r(base + math.sin(back / 20) * base * 0.03, 4),
                              "secid": None, "board": None, "tenor": None, "close": None, "waprice": None,
                              "quote_units": None, "close_per_unit": None, "waprice_per_unit": None,
                              "num_trades": None, "nominal": 1, "set_date": None, "trade_time": None, "_w": written})
        d += timedelta(days=1)
    return out


def _key(v: Any) -> Any:
    return "" if v is None else v


class HistoryHub(ThreadingHTTPServer):
    """Knobs: ``mode`` (``ok``, ``no_api``: a hub before 0.2.0, ``no_read``: a token without scope read), ``data``
    (``synthetic()``), ``requests`` (paths asked, with their If-None-Match), ``fail`` (5xx answers to give first)."""

    daemon_threads = True

    def reset(self, data: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.mode, self.fail = "ok", 0
        self.data = data if data is not None else synthetic()
        self.requests: list[tuple[str, str | None, int]] = []
        self.lock = threading.Lock()

    def touch(self, dataset: str, match, **changes: Any) -> int:
        """A correction: rows of ``dataset`` for which ``match(row)`` get ``changes`` and a new write time."""
        n = 0
        now = datetime.now(UTC).isoformat()
        for r in self.data[dataset]:
            if match(r):
                r.update(changes, _w=now)
                n += 1
        return n


class Handler(BaseHTTPRequestHandler):
    server: HistoryHub

    def log_message(self, *args) -> None:
        pass

    def _send(self, code: int, body: bytes = b"", **headers: str) -> None:
        if body and len(body) >= 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k.replace("_", "-"), v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, code: int, message: str) -> None:
        self._send(code, json.dumps({"error": {"status": code, "message": message}, "request_id": "x"}).encode())

    def do_GET(self) -> None:
        s, u = self.server, urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        tag = self.headers.get("If-None-Match")
        if u.path == "/health/ready":
            return self._send(200, json.dumps({"status": "ok", "version": "0.2.0" if s.mode != "no_api" else "0.1.3",
                                               "migrations": {}}).encode())
        if s.mode == "no_api" or not u.path.startswith("/v1/history/"):
            return self._error(404, "Not Found")
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._error(401, "unauthorized")
        if s.mode == "no_read":
            return self._error(403, "token lacks scope read")
        if s.fail:
            s.fail -= 1
            return self._error(503, "database unavailable")
        leaf = u.path.removeprefix("/v1/history/")
        with s.lock:
            s.requests.append((leaf, tag, len(s.requests)))
        if leaf == "index":
            return self._index()
        name, _, ds = leaf.partition("/")
        if name not in DATASETS or (name == "curves") != bool(ds):
            return self._error(404, f"unknown endpoint {leaf}")
        cols, order, dcol = DATASETS[name]
        if name == "curves" and ds not in {r["_ds"] for r in s.data["curves"]}:
            return self._error(404, f"unknown curve dataset {ds}")
        if "from" not in q:
            return self._error(422, "from is required")
        d1, d2 = q["from"], q.get("to") or "2099-12-31"
        if d1 > d2:
            return self._error(400, "from after to")
        limit = int(q.get("limit") or 10000)
        if not 1 <= limit <= 50000:
            return self._error(422, "limit out of range")
        keys = KEYS[name]
        if q.get("fields"):
            asked = q["fields"].split(",")
            if bad := [f for f in asked if f not in cols]:
                return self._error(400, f"unknown field {bad[0]}")
            out_cols = [c for c in cols if c in keys or c in asked]
        else:
            out_cols = list(cols)
        sel = [r for r in s.data[name] if d1 <= r[dcol] <= d2 and (name != "curves" or r["_ds"] == ds)]
        if name == "bonds" and q.get("secid"):
            secids = q["secid"].split(",")
            if len(secids) > 500:
                return self._error(400, "at most 500 secids per call")
            sel = [r for r in sel if r["secid"] in secids]
        if name == "fx" and q.get("kind"):
            kinds = q["kind"].split(",")
            sel = [r for r in sel if r["kind"] in kinds]
        if name == "fixings" and q.get("code"):
            codes = q["code"].upper().split(",")
            sel = [r for r in sel if r["index_code"] in codes]
        sel.sort(key=lambda r: tuple(_key(r.get(k)) for k in order))
        norm = json.dumps({k: v for k, v in sorted(q.items()) if k not in ("cursor", "limit")}, sort_keys=True)
        qhash = hashlib.sha256(f"{leaf}?{norm}".encode()).hexdigest()[:16]
        off = 0
        if q.get("cursor"):
            try:
                c = json.loads(base64.urlsafe_b64decode(q["cursor"] + "=" * (-len(q["cursor"]) % 4)))
            except ValueError:
                return self._error(400, "malformed cursor")
            if c.get("q") != qhash:
                return self._error(400, "cursor does not belong to this query (repeat the query without it)")
            off = c["o"]
        version = f"{len(sel)}:{max((r['_w'] for r in sel), default='')}"
        etag = 'W/"' + hashlib.sha256(f"{qhash}|{q.get('cursor')}|{limit}|{version}".encode()).hexdigest()[:24] + '"'
        if tag == etag:
            return self._send(304, ETag=etag)
        page = sel[off:off + limit]
        nxt = (base64.urlsafe_b64encode(json.dumps({"q": qhash, "o": off + limit}).encode()).decode().rstrip("=")
               if off + limit < len(sel) else None)
        body = {"dataset": name if name != "curves" else f"curves/{ds}", "columns": out_cols,
                "rows": [[r.get(c) for c in out_cols] for r in page],
                "as_of": datetime.now(UTC).isoformat(), "known_at": None,
                "data_version": max((r["_w"] for r in sel), default=None), "next": nxt,
                "meta": {"from": d1, "to": d2, "order": list(order), "limit": limit, "returned": len(page)}}
        self._send(200, json.dumps(body, separators=(",", ":")).encode(), ETag=etag)

    def _index(self) -> None:
        s = self.server
        rows = []
        for name in ("bonds", "zcyc", "fixings", "fx"):
            sel = s.data[name]
            if not sel:
                continue
            dcol = DATASETS[name][2]
            rows.append([name, None, "moex_iss", None, min(r[dcol] for r in sel), max(r[dcol] for r in sel), len(sel),
                         len({r[dcol] for r in sel}), len({r["secid"] for r in sel}) if name == "bonds" else None,
                         f"/v1/history/{name}"])
        for ds in sorted({r["_ds"] for r in s.data["curves"]}):
            sel = [r for r in s.data["curves"] if r["_ds"] == ds]
            for src in sorted({r["source"] for r in sel}):
                ss = [r for r in sel if r["source"] == src]
                rows.append(["curves", ds, src, "pct", min(r["snapshot_date"] for r in ss),
                             max(r["snapshot_date"] for r in ss), len({r["quote_set_id"] for r in ss}),
                             len({r["snapshot_date"] for r in ss}), None, f"/v1/history/curves/{ds}"])
        body = {"dataset": "index", "columns": ["dataset", "series", "source", "unit", "date_from", "date_to", "rows",
                                                "days", "series_count", "endpoint"],
                "rows": rows, "as_of": datetime.now(UTC).isoformat(), "known_at": None, "data_version": None,
                "next": None, "meta": {}}
        raw = json.dumps(body).encode()
        self._send(200, raw, ETag='W/"' + hashlib.sha256(raw).hexdigest()[:24] + '"')


def start(data: dict[str, list[dict[str, Any]]] | None = None) -> HistoryHub:
    srv = HistoryHub(("127.0.0.1", 0), Handler)
    srv.reset(data)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def url(srv: HistoryHub) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}"
