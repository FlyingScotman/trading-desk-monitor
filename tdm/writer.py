"""SDK для расчётного сервиса: запись прогона в базу и атомарная публикация.

    from tdm.writer import Run

    with Run("C:/tdm/data/tdm.sqlite", as_of="2026-10-13", run_type="EOD",
             source="calc-svc@host", lib_version="lib 4.2") as run:
        run.add_books([...]);  run.add_issuers([...])
        run.add_curves([...]); run.add_fx([...]); run.add_indices([...])
        run.add_positions([...])      # с risk / fx_delta / pnl внутри
        run.add_funding([...])        # с pnl внутри
        run.add_scenario_results("keyhike", positions=[...], funding_residual={...})  # необязательно
    # выход из with без исключения -> проверки -> status='complete' (видно в UI)
    # исключение или ошибка проверки -> status='failed' (UI продолжает показывать прошлый прогон)

Порядок вызовов важен (внешние ключи): книги, эмитенты и кривые — до позиций и фондирования.
Формат словарей — docs/DATA_CONTRACT.md, раздел «Поля».
"""
import json
import re
from datetime import date

from . import CONTRACT_VERSION
from .db import BUCKETS, connect, init_db, now_iso

RUN_TYPES = ("EOD", "INTRADAY", "TEST")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

POSITION_COLS = [
    "position_id", "book_id", "instr_type", "asset_class", "segment", "name", "isin", "issuer", "ccy",
    "maturity", "tenor_y", "coupon_type", "coupon", "index_name", "margin", "side", "notional", "notional_rub",
    "price", "mv", "mv_rub", "ytm", "spread_bp", "mdur", "spr_dur", "convexity", "dv01", "cs01", "jtd",
    "wal", "cpr", "counterparty", "new_today", "extra_json",
]
PNL_COLS = ["carry", "rolldown", "rates", "spread", "fx_gross", "fx_funding", "convexity", "new_trades", "other", "official", "break_note"]
FUNDING_COLS = ["deal_id", "book_id", "type", "ccy", "amount", "rate_expr", "rate", "start_date", "maturity", "counterparty", "collateral"]
SCEN_COLS = ["rates", "spread", "fx_gross", "fx_funding", "convexity", "total"]


class ValidationError(Exception):
    pass


def _ins(con, table, cols, rows):
    if rows:
        con.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)


def _b(v):
    return 1 if v else 0


class Run:
    """Один расчётный прогон. Пока не опубликован, UI его не видит."""

    def __init__(self, db_path=None, as_of=None, run_type="EOD", source=None, lib_version=None,
                 market_snapshot=None, comment=None, contract_version=CONTRACT_VERSION):
        if not as_of or not _DATE.match(str(as_of)):
            raise ValueError("as_of должен быть строкой 'YYYY-MM-DD'")
        date.fromisoformat(as_of)
        if run_type not in RUN_TYPES:
            raise ValueError(f"run_type должен быть одним из {RUN_TYPES}")
        init_db(db_path).close()
        self.con = connect(db_path)
        self.as_of, self.run_type = as_of, run_type
        with self.con:
            cur = self.con.execute(
                "INSERT INTO calc_run (as_of, run_type, status, contract_version, source, lib_version, market_snapshot, comment, started_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (as_of, run_type, "loading", contract_version, source, lib_version, market_snapshot, comment, now_iso()))
        self.run_id = cur.lastrowid
        self.status = "loading"
        self.warnings = []

    # ------------------------------------------------------------------ контекст
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.publish()
        else:
            self.fail(f"{exc_type.__name__}: {exc}")
        self.con.close()
        return False

    # ------------------------------------------------------------------ лог
    def log(self, level, message):
        with self.con:
            self.con.execute("INSERT INTO calc_run_log (run_id, ts, level, message) VALUES (?,?,?,?)",
                             (self.run_id, now_iso(), level, str(message)))
        if level == "WARN":
            self.warnings.append(message)

    # ------------------------------------------------------------------ статика
    def add_books(self, books):
        """[{book_id, name, desk, trader?}] — порядок списка = порядок в UI."""
        with self.con:
            _ins(self.con, "run_book", ["run_id", "book_id", "name", "desk", "trader", "ord"],
                 [(self.run_id, b["book_id"], b["name"], b["desk"], b.get("trader"), i) for i, b in enumerate(books)])

    def add_issuers(self, issuers):
        """[{issuer, issuer_group, name?, sector?, rating?, is_gov?, is_quasi?}] — issuer: ключ (как в position.issuer),
        name: отображаемое имя, если ключ — код (ИНН и т.п.)."""
        with self.con:
            for x in issuers:
                if x.get("rating"):
                    self.con.execute("INSERT OR IGNORE INTO ref_rating (rating, ord) VALUES (?, (SELECT COALESCE(MAX(ord), -1) + 1 FROM ref_rating))", (x["rating"],))
            _ins(self.con, "run_issuer", ["run_id", "issuer", "name", "issuer_group", "sector", "rating", "is_gov", "is_quasi"],
                 [(self.run_id, x["issuer"], x.get("name"), x["issuer_group"], x.get("sector"), x.get("rating"), _b(x.get("is_gov")), _b(x.get("is_quasi")))
                  for x in issuers])

    # ------------------------------------------------------------------ рынок
    def add_curves(self, curves):
        """[{curve_id, name, ccy, kind: 'bond'|'swap', points: [{bucket, rate, move_bp}]}]"""
        with self.con:
            _ins(self.con, "run_curve", ["run_id", "curve_id", "name", "ccy", "kind", "ord"],
                 [(self.run_id, c["curve_id"], c["name"], c["ccy"], c["kind"], i) for i, c in enumerate(curves)])
            _ins(self.con, "mkt_curve_point", ["run_id", "curve_id", "bucket", "rate", "move_bp"],
                 [(self.run_id, c["curve_id"], p["bucket"], p["rate"], p.get("move_bp", 0) or 0) for c in curves for p in c["points"]])

    def add_fx(self, rates):
        """[{ccy, rate (RUB за 1 ед.), move_pct}] — RUB добавляется сам."""
        rows = {r["ccy"]: r for r in rates}
        rows.setdefault("RUB", {"ccy": "RUB", "rate": 1.0, "move_pct": 0.0})
        with self.con:
            _ins(self.con, "mkt_fx", ["run_id", "ccy", "rate", "move_pct"],
                 [(self.run_id, r["ccy"], r["rate"], r.get("move_pct", 0) or 0) for r in rows.values()])

    def add_indices(self, indices):
        """[{index_name, value (%)}]"""
        with self.con:
            _ins(self.con, "mkt_index", ["run_id", "index_name", "value"], [(self.run_id, x["index_name"], x["value"]) for x in indices])

    # ------------------------------------------------------------------ из слоя входных данных
    def add_reference_data(self, books=True, issuers=True, market=True):
        """Взять статику и рынок на as_of из центрального хранилища (ref_*, md_*), а не передавать самому.

        Книги — ref_book; эмитенты — ref_issuer + рейтинг на дату; кривые — ref_curve + md_curve_point,
        интерполированные на 11 пилларов, move_bp — к предыдущей дате с кривыми; курсы — md_fx (+ изменение, %);
        индексы — последние фиксинги. Возвращает список того, чего не нашлось.
        """
        from . import market as M
        con, d, missing = self.con, self.as_of, []
        if books:
            rows = [dict(r) for r in con.execute("SELECT * FROM ref_book WHERE active=1 ORDER BY ord, book_id")]
            if not rows:
                missing.append("ref_book пуст (загрузите books.csv)")
            self.add_books(rows)
        if issuers:
            rt = M.ratings_on(con, d)
            self.add_issuers([{"issuer": r["issuer_id"], "name": r["name"], "issuer_group": r["issuer_group"], "sector": r["sector"],
                               "rating": rt.get(r["issuer_id"]), "is_gov": r["is_gov"], "is_quasi": r["is_quasi"]}
                              for r in con.execute("SELECT * FROM ref_issuer")])
        if market:
            cur, prev_d = M.curves_on(con, d), M.prev_date(con, d)
            prev = M.curves_on(con, prev_d) if prev_d else {}
            curves = []
            for c in con.execute("SELECT * FROM ref_curve ORDER BY ord, curve_id"):
                if c["curve_id"] not in cur:
                    missing.append(f"нет точек кривой {c['curve_id']} на {d}")
                    continue
                pts = cur[c["curve_id"]]
                pp = prev.get(c["curve_id"], pts)
                curves.append({"curve_id": c["curve_id"], "name": c["name"], "ccy": c["ccy"], "kind": c["kind"],
                               "points": [{"bucket": b, "rate": r, "move_bp": round((r - q) * 100, 4)}
                                          for (b, _), r, q in zip(BUCKETS, pts, pp)]})
            self.add_curves(curves)
            fx, fx_prev_d = M.fx_on(con, d), M.prev_date(con, d, "md_fx")
            fx_prev = M.fx_on(con, fx_prev_d) if fx_prev_d else {}
            if not fx:
                missing.append(f"нет курсов на {d}")
            self.add_fx([{"ccy": k, "rate": v, "move_pct": (v / fx_prev[k] - 1) * 100 if fx_prev.get(k) else 0} for k, v in fx.items()])
            self.add_indices([{"index_name": k, "value": v} for k, v in M.fixings_on(con, d).items()])
        for m in missing:
            self.log("WARN", m)
        return missing

    # ------------------------------------------------------------------ позиции
    def add_positions(self, positions):
        """[{...поля position..., risk: {curve_id: {bucket: dv01}}, fx_delta: {ccy: amount}, pnl: {...}}]

        Можно вызывать много раз (порциями). Отсутствующие необязательные поля = NULL.
        """
        pos, risk, fx, pnl = [], [], [], []
        for p in positions:
            row = dict(p)
            extra = row.pop("extra", None)
            if extra is not None:
                row["extra_json"] = json.dumps(extra, ensure_ascii=False)
            row["new_today"] = _b(row.get("new_today"))
            for k in ("cs01", "jtd"):
                if row.get(k) is None:
                    row[k] = 0.0
            pos.append([self.run_id] + [row.get(c) for c in POSITION_COLS])
            pid = p["position_id"]
            for curve, buckets in (p.get("risk") or {}).items():
                for bucket, dv in buckets.items():
                    if dv:
                        risk.append((self.run_id, pid, curve, bucket, dv))
            for ccy, d in (p.get("fx_delta") or {}).items():
                if d:
                    fx.append((self.run_id, pid, ccy, d))
            if p.get("pnl") is not None:
                q = p["pnl"]
                pnl.append([self.run_id, pid] + [q.get(c) if c in ("official", "break_note") else (q.get(c) or 0) for c in PNL_COLS])
        with self.con:
            _ins(self.con, "position", ["run_id"] + POSITION_COLS, pos)
            _ins(self.con, "position_risk", ["run_id", "position_id", "curve_id", "bucket", "dv01"], risk)
            _ins(self.con, "position_fx", ["run_id", "position_id", "ccy", "delta"], fx)
            _ins(self.con, "position_pnl", ["run_id", "position_id"] + PNL_COLS, pnl)

    # ------------------------------------------------------------------ фондирование
    def add_funding(self, deals):
        """[{deal_id, book_id, type, ccy, amount, rate_expr?, rate, start_date?, maturity, counterparty?, collateral?,
             pnl: {carry, fx_gross, fx_allocated}}]"""
        with self.con:
            _ins(self.con, "funding_deal", ["run_id"] + FUNDING_COLS, [[self.run_id] + [d.get(c) for c in FUNDING_COLS] for d in deals])
            _ins(self.con, "funding_pnl", ["run_id", "deal_id", "carry", "fx_gross", "fx_allocated"],
                 [(self.run_id, d["deal_id"], (d.get("pnl") or {}).get("carry", 0) or 0, (d.get("pnl") or {}).get("fx_gross", 0) or 0,
                   (d.get("pnl") or {}).get("fx_allocated", 0) or 0) for d in deals])

    # ------------------------------------------------------------------ сценарии
    def add_scenario_results(self, scenario_id, positions, funding_residual=None):
        """Полная переоценка сценария. positions: [{position_id, rates, spread, fx_gross, fx_funding, convexity, total}];
        funding_residual: {book_id: RUB} — FX фондирования, не разнесённый на позиции."""
        with self.con:
            _ins(self.con, "scenario_result", ["run_id", "scenario_id", "position_id"] + SCEN_COLS,
                 [[self.run_id, scenario_id, r["position_id"]] + [r.get(c, 0) or 0 for c in SCEN_COLS] for r in positions])
            _ins(self.con, "scenario_funding_result", ["run_id", "scenario_id", "book_id", "fx_residual"],
                 [(self.run_id, scenario_id, b, v) for b, v in (funding_residual or {}).items()])

    # ------------------------------------------------------------------ публикация
    def validate(self):
        """Ошибки -> ValidationError; предупреждения -> лог прогона (WARN)."""
        con, rid, err = self.con, self.run_id, []
        q = lambda sql, *a: con.execute(sql, (rid,) + a).fetchall()  # noqa: E731
        if not q("SELECT 1 FROM position WHERE run_id=? LIMIT 1"):
            err.append("нет ни одной позиции")
        for r in q("SELECT DISTINCT p.issuer FROM position p LEFT JOIN run_issuer i ON i.run_id=p.run_id AND i.issuer=p.issuer"
                   " WHERE p.run_id=? AND p.issuer IS NOT NULL AND i.issuer IS NULL"):
            err.append(f"эмитент '{r[0]}' не загружен в add_issuers")
        for r in q("SELECT DISTINCT p.position_id FROM position p WHERE p.run_id=? AND p.instr_type IN ('BOND','CDS') AND p.issuer IS NULL"):
            err.append(f"позиция {r[0]}: BOND/CDS без эмитента")
        used = q("SELECT ccy FROM position WHERE run_id=? UNION SELECT ccy FROM position_fx WHERE run_id=?"
                 " UNION SELECT ccy FROM funding_deal WHERE run_id=? UNION SELECT ccy FROM run_curve WHERE run_id=?", rid, rid, rid)
        have = {r[0] for r in q("SELECT ccy FROM mkt_fx WHERE run_id=?")}
        for r in used:
            if r[0] not in have:
                err.append(f"нет курса для валюты {r[0]} (add_fx)")
        n_b = con.execute("SELECT COUNT(*) FROM ref_bucket").fetchone()[0]
        for r in q("SELECT curve_id, COUNT(*) FROM mkt_curve_point WHERE run_id=? GROUP BY curve_id HAVING COUNT(*) <> ?", n_b):
            err.append(f"кривая {r[0]}: {r[1]} точек вместо {n_b} (нужен каждый бакет)")
        for r in q("SELECT c.curve_id FROM run_curve c WHERE c.run_id=? AND NOT EXISTS"
                   " (SELECT 1 FROM mkt_curve_point m WHERE m.run_id=c.run_id AND m.curve_id=c.curve_id)"):
            err.append(f"кривая {r[0]}: нет точек")
        # предупреждения
        for r in q("SELECT p.position_id, p.dv01, COALESCE(SUM(r.dv01),0) FROM position p LEFT JOIN position_risk r"
                   " ON r.run_id=p.run_id AND r.position_id=p.position_id WHERE p.run_id=? GROUP BY p.position_id"):
            if abs(r[1] - r[2]) > max(100.0, 1e-3 * abs(r[1])):
                self.log("WARN", f"позиция {r[0]}: dv01={r[1]:.0f}, сумма position_risk={r[2]:.0f}")
        n_nopnl = q("SELECT COUNT(*) FROM position p WHERE p.run_id=? AND NOT EXISTS"
                    " (SELECT 1 FROM position_pnl x WHERE x.run_id=p.run_id AND x.position_id=p.position_id)")[0][0]
        if n_nopnl:
            self.log("WARN", f"{n_nopnl} позиций без PnL (в UI PnL = 0)")
        n_noff = q("SELECT COUNT(*) FROM position_pnl WHERE run_id=? AND official IS NULL")[0][0]
        if n_noff:
            self.log("WARN", f"{n_noff} позиций без официального PnL (unexplained = 0)")
        for r in q("SELECT d.book_id, SUM(f.fx_allocated), (SELECT COALESCE(SUM(x.fx_funding),0) FROM position_pnl x JOIN position p"
                   " ON p.run_id=x.run_id AND p.position_id=x.position_id WHERE x.run_id=d.run_id AND p.book_id=d.book_id)"
                   " FROM funding_deal d JOIN funding_pnl f ON f.run_id=d.run_id AND f.deal_id=d.deal_id WHERE d.run_id=? GROUP BY d.book_id"):
            if abs((r[1] or 0) - (r[2] or 0)) > max(1000.0, 1e-3 * abs(r[1] or 0)):
                self.log("WARN", f"книга {r[0]}: funding fx_allocated={r[1]:.0f}, сумма position fx_funding={r[2]:.0f}")
        if err:
            raise ValidationError("; ".join(err[:50]) + (f" ... и ещё {len(err) - 50}" if len(err) > 50 else ""))

    def fill_official_pnl(self):
        """Где расчётный сервис не дал official, взять PnL позиции из загруженного pnl_official (тот же position_id)."""
        with self.con:
            n = self.con.execute(
                "UPDATE position_pnl SET official = (SELECT o.pnl_rub FROM pnl_official o WHERE o.as_of=? AND o.position_id=position_pnl.position_id)"
                " WHERE run_id=? AND official IS NULL AND EXISTS (SELECT 1 FROM pnl_official o WHERE o.as_of=? AND o.position_id=position_pnl.position_id)",
                (self.as_of, self.run_id, self.as_of)).rowcount
        if n:
            self.log("INFO", f"официальный PnL взят из pnl_official для {n} позиций")
        return n

    def publish(self):
        self.fill_official_pnl()
        try:
            self.validate()
        except ValidationError as e:
            self.fail(f"проверка не пройдена: {e}")
            raise
        with self.con:
            self.con.execute("UPDATE calc_run SET status='complete', published_at=? WHERE run_id=?", (now_iso(), self.run_id))
            if self.run_type == "EOD":
                _store_history(self.con, self.run_id, self.as_of)
        self.status = "complete"
        self.log("INFO", "опубликован")

    def fail(self, reason):
        with self.con:
            self.con.execute("UPDATE calc_run SET status='failed', error=? WHERE run_id=?", (str(reason)[:4000], self.run_id))
        self.status = "failed"
        self.log("ERROR", reason)


# ---------------------------------------------------------------------- глобальные данные
def run_totals(con, run_id):
    """Итоги деска по прогону: (pnl_official, pnl_explained, dv01, cs01). Фондирование входит в PnL."""
    p = con.execute(
        "SELECT COALESCE(SUM(p.dv01),0), COALESCE(SUM(p.cs01),0),"
        " COALESCE(SUM(x.carry+x.rolldown+x.rates+x.spread+x.fx_gross+x.fx_funding+x.convexity+x.new_trades+x.other),0),"
        " COALESCE(SUM(COALESCE(x.official, x.carry+x.rolldown+x.rates+x.spread+x.fx_gross+x.fx_funding+x.convexity+x.new_trades+x.other)),0)"
        " FROM position p LEFT JOIN position_pnl x ON x.run_id=p.run_id AND x.position_id=p.position_id WHERE p.run_id=?",
        (run_id,)).fetchone()
    f = con.execute("SELECT COALESCE(SUM(carry + fx_gross - fx_allocated),0) FROM funding_pnl WHERE run_id=?", (run_id,)).fetchone()[0]
    return p[3] + f, p[2] + f, p[0], p[1]


def _store_history(con, run_id, as_of):
    off, exp, dv, cs = run_totals(con, run_id)
    con.execute("INSERT INTO desk_history (as_of, run_id, pnl_official, pnl_explained, dv01, cs01, updated_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(as_of) DO UPDATE SET run_id=excluded.run_id, pnl_official=excluded.pnl_official,"
                " pnl_explained=excluded.pnl_explained, dv01=excluded.dv01, cs01=excluded.cs01, updated_at=excluded.updated_at",
                (as_of, run_id, off, exp, dv, cs, now_iso()))


def upsert_scenarios(db_path, scenarios):
    """[{scenario_id, name, definition: {curves?, fx?, spreads?}, active?}] — порядок списка = порядок в UI."""
    con = init_db(db_path)
    with con:
        for i, s in enumerate(scenarios):
            con.execute("INSERT INTO scenario_def (scenario_id, name, ord, definition_json, active, updated_at) VALUES (?,?,?,?,?,?)"
                        " ON CONFLICT(scenario_id) DO UPDATE SET name=excluded.name, ord=excluded.ord,"
                        " definition_json=excluded.definition_json, active=excluded.active, updated_at=excluded.updated_at",
                        (s["scenario_id"], s["name"], s.get("ord", i), json.dumps(s["definition"], ensure_ascii=False),
                         1 if s.get("active", True) else 0, now_iso()))
    con.close()


def load_history(db_path, rows):
    """Итоги прошлых дней без детализации: [{as_of, pnl_official, pnl_explained, dv01, cs01}].
    Не перезаписывает дни, у которых уже есть опубликованный EOD-прогон."""
    con = init_db(db_path)
    with con:
        for r in rows:
            con.execute("INSERT INTO desk_history (as_of, run_id, pnl_official, pnl_explained, dv01, cs01, updated_at) VALUES (?,NULL,?,?,?,?,?)"
                        " ON CONFLICT(as_of) DO UPDATE SET pnl_official=excluded.pnl_official, pnl_explained=excluded.pnl_explained,"
                        " dv01=excluded.dv01, cs01=excluded.cs01, updated_at=excluded.updated_at WHERE desk_history.run_id IS NULL",
                        (r["as_of"], r["pnl_official"], r["pnl_explained"], r["dv01"], r["cs01"], now_iso()))
    con.close()


def attach_scenario_results(db_path, run_id, scenario_id, positions, funding_residual=None, replace=True):
    """Приложить полную переоценку сценария к УЖЕ опубликованному прогону (уровень 2, если пересчёт
    сценариев идёт после публикации рисков). Риски и PnL прогона не меняются; монитор подхватит
    результат при следующем открытии страницы. replace=True — заменить прежний результат этого сценария."""
    con = init_db(db_path)
    try:
        run = con.execute("SELECT status FROM calc_run WHERE run_id=?", (run_id,)).fetchone()
        if not run or run[0] != "complete":
            raise ValueError(f"прогон {run_id} не найден или не опубликован")
        if not con.execute("SELECT 1 FROM scenario_def WHERE scenario_id=?", (scenario_id,)).fetchone():
            raise ValueError(f"сценарий {scenario_id} не определён (upsert_scenarios / build-hist-scenarios)")
        with con:
            if replace:
                con.execute("DELETE FROM scenario_result WHERE run_id=? AND scenario_id=?", (run_id, scenario_id))
                con.execute("DELETE FROM scenario_funding_result WHERE run_id=? AND scenario_id=?", (run_id, scenario_id))
            _ins(con, "scenario_result", ["run_id", "scenario_id", "position_id"] + SCEN_COLS,
                 [[run_id, scenario_id, r["position_id"]] + [r.get(c, 0) or 0 for c in SCEN_COLS] for r in positions])
            _ins(con, "scenario_funding_result", ["run_id", "scenario_id", "book_id", "fx_residual"],
                 [(run_id, scenario_id, b, v) for b, v in (funding_residual or {}).items()])
            con.execute("INSERT INTO calc_run_log (run_id, ts, level, message) VALUES (?,?,?,?)",
                        (run_id, now_iso(), "INFO", f"приложена полная переоценка сценария {scenario_id}: {len(positions)} позиций"))
    finally:
        con.close()
