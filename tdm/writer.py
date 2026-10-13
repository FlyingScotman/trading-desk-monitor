"""SDK для расчётного сервиса: запись прогона в базу и атомарная публикация.

    from tdm.writer import Run

    with Run("C:/tdm/data/tdm.sqlite", as_of="2026-10-13", run_type="EOD",
             source="calc-svc@host", lib_version="lib 4.2") as run:
        run.add_books([...]);  run.add_issuers([...])          # книги с book_type / entity / ocp_to (1.3)
        run.add_curves([...]); run.add_fx([...]); run.add_indices([...])   # курсы: рыночный и ЦБ (1.3)
        run.add_funding_rules([...])  # справочник ставок фондирования (1.3)
        run.add_positions([...])      # с risk / fx_delta / pnl внутри
        run.add_funding([...])        # строки POS / HTM / CASH / REPO с pnl внутри
        run.derive_funding()          # или: строки, которых расчётный сервис не дал, строит tdm (1.3)
        run.add_ocp_transfers()       # сводка передачи ОВП (1.3)
        run.add_scenario_results("keyhike", positions=[...], funding_residual={...})  # необязательно
    # выход из with без исключения -> проверки -> status='complete' (видно в UI)
    # исключение или ошибка проверки -> status='failed' (UI продолжает показывать прошлый прогон)

    attach_official_pnl(db, run_id)   # T+1: официальный PnL из pnl_official — к опубликованному EOD-прогону

Порядок вызовов важен (внешние ключи): книги, эмитенты и кривые — до позиций и фондирования.
Формат словарей — docs/DATA_CONTRACT.md, раздел «Поля» и §0 (версия 1.3).
"""
import json
import re
from datetime import date

from . import CONTRACT_VERSION
from .db import BUCKETS, connect, init_db, now_iso

RUN_TYPES = ("EOD", "INTRADAY", "TEST")
BOOK_TYPES = ("TRADING", "HTM", "DERIVS")
FUNDING_KINDS = ("POS", "HTM", "CASH", "REPO", "DEAL")   # DEAL — сделка книги в форме 1.2
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

POSITION_COLS = [
    "position_id", "book_id", "instr_type", "asset_class", "segment", "name", "isin", "issuer", "ccy",
    "maturity", "tenor_y", "coupon_type", "coupon", "index_name", "margin", "side", "notional", "notional_rub",
    "price", "mv", "mv_rub", "ytm", "spread_bp", "mdur", "spr_dur", "convexity", "dv01", "cs01", "jtd",
    "wal", "cpr", "counterparty", "new_today", "extra_json",
    "instrument_id", "accrued", "coupon_freq", "yield_date", "spread_type", "spread_curve",
    "ofz_spr01", "accrual_only", "purchase_date", "purchase_price", "purchase_yield", "contract_rate", "ocp_transfer",
]
SPREAD_TYPES = ("Z", "G", "I", "DM", "OAS", "ASW")
PNL_COLS = ["carry", "rolldown", "rates", "spread", "fx_gross", "fx_funding", "convexity", "new_trades", "other", "official", "break_note",
            "funding", "fx_gross_cbr", "fx_funding_cbr"]
PNL_NULLABLE = ("official", "break_note", "fx_gross_cbr", "fx_funding_cbr")
FUNDING_COLS = ["deal_id", "book_id", "type", "ccy", "amount", "rate_expr", "rate", "start_date", "maturity", "counterparty", "collateral",
                "kind", "position_id", "index_name", "spread_bp", "fixed_rate", "benchmark", "day_basis", "rule_note"]
RULE_COLS = ["book_type", "desk", "book_id", "entity", "instrument_id", "ccy", "index_name", "spread_bp", "fixed_rate", "note"]
SCEN_COLS = ["rates", "spread", "fx_gross", "fx_funding", "convexity", "fx_cross", "total"]
GAMMA_FACTOR = re.compile(r"^(IR\|[^|]+\|[^|]+|CS|FX\|[A-Z]{3})$")

# Конвенции фондирования (docs/ORG_CONVENTIONS.md §1): O/N-индексы остатков ДС, базы начисления, бенчмарки
ON_INDEX = {"RUB": "RUONIA", "CNY": "RUSFAR CNY", "USD": "SOFR", "EUR": "€STR", "CHF": "SARON"}
DAY_BASIS = {"RUB": 365, "CNY": 365, "USD": 360, "EUR": 360, "CHF": 360}   # остатки ДС и РЕПО; POS / HTM — 365 (процент в RUB)
BENCH_POS = {"RUB": "КС", "CNY": "RUSFAR CNY"}
RULE_WEIGHTS = (("instrument_id", 16), ("book_id", 8), ("entity", 4), ("desk", 2), ("book_type", 1))
OCP_WARN_RUB = 10e6        # порог остатка ОВП книги TRADING после передачи (предупреждение)
FX_FUNDING_TOL = 1.0       # fx_funding бумаги = fx_allocated её строки, ±1 ₽


class ValidationError(Exception):
    pass


def _ins(con, table, cols, rows):
    if rows:
        con.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)


def _b(v):
    return 1 if v else 0


def _nz(v):
    """'' и None -> None (пустое поле справочника = «любое значение»)."""
    return None if v is None or v == "" else v


def _ver(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:2])
    except ValueError:
        return (0, 0)


# ---------------------------------------------------------------------- справочник ставок
def resolve_funding_rule(rules, book, ccy, position=None):
    """Самое конкретное подходящее правило (docs/DATA_CONTRACT.md §0.5).

    rules — [{book_type, desk, book_id, entity, instrument_id, ccy, index_name, spread_bp, fixed_rate, note}],
    book — {book_id, book_type, desk, entity}; position — {instrument_id, isin, name} или None.
    Пустое поле правила = «любое значение». Вес: бумага 16 > книга 8 > юрлицо 4 > деск 2 > режим книги 1;
    при равном весе — правило выше по списку."""
    keys = {position.get(k) for k in ("instrument_id", "isin", "name")} - {None, ""} if position else set()
    best, bw = None, -1
    for r in rules:
        if _nz(r.get("ccy")) and r["ccy"] != ccy:
            continue
        if any(_nz(r.get(k)) and r[k] != book.get(k) for k in ("book_type", "desk", "book_id", "entity")):
            continue
        if _nz(r.get("instrument_id")) and r["instrument_id"] not in keys:
            continue
        w = sum(v for k, v in RULE_WEIGHTS if _nz(r.get(k)))
        if w > bw:
            best, bw = r, w
    return best


def rule_expr(r):
    """Подпись ставки: 'КС + 0.30%', '5.00% фикс', 'RUONIA'."""
    if _nz(r.get("fixed_rate")) is not None:
        return f"{r['fixed_rate']:.2f}% фикс"
    s = r.get("spread_bp") or 0
    return r["index_name"] + ((" + " if s > 0 else " − ") + f"{abs(s) / 100:.2f}%" if s else "")


class Run:
    """Один расчётный прогон. Пока не опубликован, UI его не видит."""

    def __init__(self, db_path=None, as_of=None, run_type="EOD", source=None, lib_version=None,
                 market_snapshot=None, comment=None, contract_version=CONTRACT_VERSION,
                 prev_date=None, accrual_days=None, jtd_recovery=None, ofz_spread_move=None):
        """prev_date — прошлый рабочий день (по умолчанию из ref_calendar, иначе пн–пт); accrual_days — календарных
        дней начисления (по умолчанию as_of − prev_date); jtd_recovery — recovery JTD облигаций (по умолчанию 0)."""
        if not as_of or not _DATE.match(str(as_of)):
            raise ValueError("as_of должен быть строкой 'YYYY-MM-DD'")
        date.fromisoformat(as_of)
        if run_type not in RUN_TYPES:
            raise ValueError(f"run_type должен быть одним из {RUN_TYPES}")
        from . import market as M
        init_db(db_path).close()
        self.con = connect(db_path)
        self.as_of, self.run_type, self.contract_version = as_of, run_type, contract_version
        self.prev_date = prev_date or M.prev_business_day(self.con, as_of)
        if accrual_days is None:
            accrual_days = max(1, (date.fromisoformat(as_of) - date.fromisoformat(self.prev_date)).days)
        self.accrual_days = accrual_days
        with self.con:
            cur = self.con.execute(
                "INSERT INTO calc_run (as_of, run_type, status, contract_version, source, lib_version, market_snapshot, comment, started_at,"
                " prev_date, accrual_days, jtd_recovery, ofz_spread_move) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (as_of, run_type, "loading", contract_version, source, lib_version, market_snapshot, comment, now_iso(),
                 self.prev_date, accrual_days, 0.0 if jtd_recovery is None else jtd_recovery, ofz_spread_move))
        self.run_id = cur.lastrowid
        self.status = "loading"
        self.warnings = []

    # ------------------------------------------------------------------ контекст
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.publish()
            else:
                self.fail(f"{exc_type.__name__}: {exc}")
        finally:
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
        """[{book_id, name, desk, trader?, book_type, entity, ocp_to?}] — порядок списка = порядок в UI.
        book_type: TRADING | HTM | DERIVS (режим фондирования); ocp_to — книга деривативов для вечерней передачи ОВП."""
        with self.con:
            _ins(self.con, "run_book", ["run_id", "book_id", "name", "desk", "trader", "ord", "book_type", "entity", "ocp_to"],
                 [(self.run_id, b["book_id"], b["name"], b["desk"], b.get("trader"), i, _nz(b.get("book_type")), _nz(b.get("entity")),
                   _nz(b.get("ocp_to"))) for i, b in enumerate(books)])

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

    def add_funding_rules(self, rules):
        """Справочник ставок фондирования на прогон: [{book_type?, desk?, book_id?, entity?, instrument_id?, ccy,
        index_name? + spread_bp? | fixed_rate?, note?}]. Пустое поле = «любое значение» (§0.5)."""
        with self.con:
            n0 = self.con.execute("SELECT COALESCE(MAX(ord), -1) + 1 FROM funding_rule WHERE run_id=?", (self.run_id,)).fetchone()[0]
            _ins(self.con, "funding_rule", ["run_id", "ord"] + RULE_COLS,
                 [[self.run_id, n0 + i] + [_nz(r.get(c)) for c in RULE_COLS] for i, r in enumerate(rules)])

    # ------------------------------------------------------------------ рынок
    def add_curves(self, curves):
        """[{curve_id, name, ccy, kind: 'bond'|'swap', points: [{bucket, rate, move_bp}]}]"""
        with self.con:
            _ins(self.con, "run_curve", ["run_id", "curve_id", "name", "ccy", "kind", "ord"],
                 [(self.run_id, c["curve_id"], c["name"], c["ccy"], c["kind"], i) for i, c in enumerate(curves)])
            _ins(self.con, "mkt_curve_point", ["run_id", "curve_id", "bucket", "rate", "move_bp"],
                 [(self.run_id, c["curve_id"], p["bucket"], p["rate"], p.get("move_bp", 0) or 0) for c in curves for p in c["points"]])

    def add_fx(self, rates):
        """[{ccy, rate (рыночный, RUB за 1 ед.), move_pct, rate_cbr?, move_pct_cbr?, rate_cbr_next?}] — RUB добавляется сам.
        rate_cbr — курс ЦБ, действующий на as_of; rate_cbr_next — установленный в as_of на завтра (передача ОВП)."""
        rows = {r["ccy"]: r for r in rates}
        rows.setdefault("RUB", {"ccy": "RUB", "rate": 1.0, "move_pct": 0.0, "rate_cbr": 1.0, "move_pct_cbr": 0.0, "rate_cbr_next": 1.0})
        with self.con:
            _ins(self.con, "mkt_fx", ["run_id", "ccy", "rate", "move_pct", "rate_cbr", "move_pct_cbr", "rate_cbr_next"],
                 [(self.run_id, r["ccy"], r["rate"], r.get("move_pct", 0) or 0, r.get("rate_cbr"), r.get("move_pct_cbr"), r.get("rate_cbr_next"))
                  for r in rows.values()])

    def add_indices(self, indices):
        """[{index_name, value (%)}]"""
        with self.con:
            _ins(self.con, "mkt_index", ["run_id", "index_name", "value"], [(self.run_id, x["index_name"], x["value"]) for x in indices])

    # ------------------------------------------------------------------ из слоя входных данных
    def add_reference_data(self, books=True, issuers=True, market=True, funding_rules=True):
        """Взять статику и рынок на as_of из центрального хранилища (ref_*, md_*), а не передавать самому.

        Книги — ref_book (с book_type, entity, ocp_to); эмитенты — ref_issuer + рейтинг на дату; кривые — ref_curve +
        md_curve_point, интерполированные на 11 пилларов, move_bp — к предыдущей дате с кривыми; курсы — md_fx (+ изменение, %)
        и курсы ЦБ из md_fx_cbr (действующий на as_of, изменение к прошлому рабочему дню, курс на завтра); индексы —
        последние фиксинги; справочник ставок фондирования — ref_funding_rate. Возвращает список того, чего не нашлось.
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
            cbr, cbr_next, cbr_move = M.fx_cbr_on(con, d), M.fx_cbr_next(con, d), M.fx_cbr_move(con, d, self.prev_date)
            if fx and not cbr:
                missing.append(f"нет курсов ЦБ на {d} (md_fx_cbr): официальный базис = рыночный")
            self.add_fx([{"ccy": k, "rate": v, "move_pct": (v / fx_prev[k] - 1) * 100 if fx_prev.get(k) else 0,
                          "rate_cbr": cbr.get(k), "move_pct_cbr": cbr_move.get(k), "rate_cbr_next": cbr_next.get(k)} for k, v in fx.items()])
            self.add_indices([{"index_name": k, "value": v} for k, v in M.fixings_on(con, d).items()])
        if funding_rules:
            rules = [{c: _nz(r[c]) for c in RULE_COLS} for r in con.execute("SELECT * FROM ref_funding_rate ORDER BY rowid")]
            self.add_funding_rules(rules)
        for m in missing:
            self.log("WARN", m)
        return missing

    # ------------------------------------------------------------------ позиции
    def add_positions(self, positions):
        """[{...поля position..., risk: {curve_id: {bucket: dv01}}, fx_delta: {ccy: amount}, pnl: {...},
             gamma: [[factor_1, factor_2, d2PV], ...]}]

        gamma — вторые производные (docs/DATA_CONTRACT.md §4.3.3): факторы 'IR|<curve>|<bucket>' (1 б.п.),
        'CS' (свой спред, 1 б.п.), 'FX|<ccy>' (1 %); порядок факторов в паре не важен, пара указывается один раз.
        1.3: ofz_spr01; HTM — accrual_only, purchase_date, purchase_price, purchase_yield, contract_rate;
        сделка передачи ОВП — ocp_transfer (или extra.ocp_transfer); pnl.funding, pnl.fx_gross_cbr, pnl.fx_funding_cbr.
        Можно вызывать много раз (порциями). Отсутствующие необязательные поля = NULL.
        """
        pos, risk, fx, pnl, gam = [], [], [], [], {}
        for p in positions:
            row = dict(p)
            extra = row.pop("extra", None)
            if extra is not None:
                row["extra_json"] = json.dumps(extra, ensure_ascii=False)
            row["new_today"] = _b(row.get("new_today"))
            row["accrual_only"] = _b(row.get("accrual_only"))
            row["ocp_transfer"] = _b(row.get("ocp_transfer") or (extra or {}).get("ocp_transfer"))
            for k in ("cs01", "jtd", "ofz_spr01"):
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
            for g in p.get("gamma") or []:
                a, b, v = g
                if not v:
                    continue
                key = (pid,) + tuple(sorted((a, b)))
                if key in gam and gam[key] != v:
                    raise ValueError(f"позиция {pid}: пара гамма-факторов {key[1:]} задана дважды с разными значениями")
                gam[key] = v
            if p.get("pnl") is not None:
                q = p["pnl"]
                pnl.append([self.run_id, pid] + [q.get(c) if c in PNL_NULLABLE else (q.get(c) or 0) for c in PNL_COLS])
        with self.con:
            _ins(self.con, "position", ["run_id"] + POSITION_COLS, pos)
            _ins(self.con, "position_risk", ["run_id", "position_id", "curve_id", "bucket", "dv01"], risk)
            _ins(self.con, "position_fx", ["run_id", "position_id", "ccy", "delta"], fx)
            _ins(self.con, "position_pnl", ["run_id", "position_id"] + PNL_COLS, pnl)
            _ins(self.con, "position_gamma", ["run_id", "position_id", "factor_1", "factor_2", "gamma"],
                 [(self.run_id,) + k + (v,) for k, v in gam.items()])

    # ------------------------------------------------------------------ фондирование
    def add_funding(self, lines):
        """Строки фондирования (1.3, §0.4): [{deal_id, book_id, kind: POS|HTM|CASH|REPO, position_id? (POS, HTM), type, ccy,
        amount, index_name?, spread_bp?, fixed_rate?, rate, rate_expr?, benchmark?, day_basis?, start_date?, maturity,
        counterparty?, collateral?, pnl: {carry, fx_gross, fx_allocated, fx_gross_cbr?}}].
        Сделки в форме 1.2 (без kind и position_id) принимаются как kind='DEAL'."""
        rows, pnl = [], []
        for d in lines:
            x = dict(d)
            x["kind"] = x.get("kind") or "DEAL"
            rows.append([self.run_id] + [x.get(c) for c in FUNDING_COLS])
            q = d.get("pnl") or {}
            pnl.append((self.run_id, d["deal_id"], q.get("carry", 0) or 0, q.get("fx_gross", 0) or 0, q.get("fx_allocated", 0) or 0,
                        q.get("fx_gross_cbr")))
        with self.con:
            _ins(self.con, "funding_deal", ["run_id"] + FUNDING_COLS, rows)
            _ins(self.con, "funding_pnl", ["run_id", "deal_id", "carry", "fx_gross", "fx_allocated", "fx_gross_cbr"], pnl)

    def funding_rules(self):
        """Справочник ставок прогона (funding_rule), а если он пуст — текущий ref_funding_rate."""
        rows = self.con.execute("SELECT * FROM funding_rule WHERE run_id=? ORDER BY ord", (self.run_id,)).fetchall() or \
            self.con.execute("SELECT * FROM ref_funding_rate ORDER BY rowid").fetchall()
        return [{c: _nz(r[c]) for c in RULE_COLS} for r in rows]

    def derive_funding(self, kinds=("POS", "HTM", "CASH", "REPO")):
        """Построить строки фондирования, которых расчётный сервис не дал (docs/SERVICE_DATA_MODEL.md §3.3).

        POS  — облигации книг TRADING без своей строки: тело = −чистая MV (mv − notional × accrued / 100) в валюте бумаги,
               ставка — правило справочника (§0.5); процент — в RUB по курсу ЦБ, база 365;
        HTM  — облигации книг HTM (accrual_only): тело = −notional × purchase_price / 100, контрактная ставка, база 365;
        CASH — остатки cash_snapshot книг прогона × O/N своей валюты (RUB, CNY — 365; USD, EUR, CHF — 360), по рыночному курсу;
        REPO — сделки fund_snapshot на as_of (РЕПО деска РЕПО против книги, особые сделки), по рыночному курсу.
        Начисление — за календарные дни (prev_date, as_of], ставка каждого дня — индекс, действующий в этот день
        (md_fixing; если ряда нет — mkt_index прогона), плюс спред. FX-переоценка тела: рыночная и по курсам ЦБ.
        Бумагам POS / HTM заполняется position_pnl.funding, fx_funding (= fx_allocated строки) и fx_funding_cbr.
        CASH и REPO строятся, только если строк этого вида в прогоне ещё нет. Возвращает {kind: число строк}."""
        from . import market as M
        con, rid, d = self.con, self.run_id, self.as_of
        days = M.calendar_days(self.prev_date, d) or [d]
        nxt = M.next_business_day(con, d)
        books = {r["book_id"]: dict(r) for r in con.execute("SELECT * FROM run_book WHERE run_id=?", (rid,))}
        fx = {r["ccy"]: dict(r) for r in con.execute("SELECT * FROM mkt_fx WHERE run_id=?", (rid,))}
        idx_run = {r[0]: r[1] for r in con.execute("SELECT index_name, value FROM mkt_index WHERE run_id=?", (rid,))}
        rules = self.funding_rules()
        have_ids = {r[0] for r in con.execute("SELECT deal_id FROM funding_deal WHERE run_id=?", (rid,))}
        covered = {r[0] for r in con.execute("SELECT position_id FROM funding_deal WHERE run_id=? AND position_id IS NOT NULL", (rid,))}
        kinds_have = {r[0] for r in con.execute("SELECT DISTINCT kind FROM funding_deal WHERE run_id=?", (rid,))}
        cache, problems = {}, []

        def daily(index, spread, fixed):
            """Ставки по дням периода, %."""
            if fixed is not None:
                return [fixed] * len(days)
            if index not in cache:   # день без фиксинга в ряду (ряд короче периода) — значение из mkt_index прогона
                ser = [M.fixing_on(con, index, x) for x in days]
                ser = [idx_run.get(index) if v is None else v for v in ser]
                cache[index] = None if any(v is None for v in ser) else ser
            if cache[index] is None:
                return None
            return [v + (spread or 0) / 100 for v in cache[index]]

        def fxr(ccy, cbr=False):
            r = fx.get(ccy) or {}
            rate = (r.get("rate_cbr") if cbr else None) or r.get("rate")
            mv = r.get("move_pct_cbr") if cbr and r.get("move_pct_cbr") is not None else r.get("move_pct")
            return rate, mv or 0.0

        def reval(amount, ccy, cbr=False):
            rate, mv = fxr(ccy, cbr)
            return 0.0 if ccy == "RUB" or rate is None else amount * rate * mv / 100 / (1 + mv / 100)

        lines, pos_upd = [], []

        def add(kind, book, pid, ccy, amount, rule, basis, conv_cbr, extra):
            fixed = _nz(rule.get("fixed_rate"))
            rates = daily(rule.get("index_name"), rule.get("spread_bp"), fixed)
            if rates is None:
                problems.append(f"{kind} {pid or book + ' ' + ccy}: нет значения индекса {rule.get('index_name')}")
                return
            conv = fxr(ccy, conv_cbr)[0]
            if conv is None:
                problems.append(f"{kind} {pid or book}: нет курса {ccy}")
                return
            carry = amount * conv * sum(rates) / 100 / basis
            gross, gross_cbr = (reval(amount, ccy), reval(amount, ccy, True)) if kind != "HTM" else (0.0, 0.0)
            alloc = gross if pid else 0.0
            line = {"book_id": book, "kind": kind, "position_id": pid, "ccy": ccy, "amount": amount,
                    "index_name": None if fixed is not None else rule.get("index_name"), "spread_bp": 0 if fixed is not None else (rule.get("spread_bp") or 0),
                    "fixed_rate": fixed, "rate": rates[-1], "rate_expr": rule_expr(rule), "rule_note": rule.get("note"), "day_basis": basis,
                    "start_date": d, "maturity": nxt, "counterparty": "Казначейство",
                    "pnl": {"carry": carry, "fx_gross": gross, "fx_allocated": alloc, "fx_gross_cbr": gross_cbr if ccy != "RUB" else None}}
            line.update(extra)
            lines.append(line)
            if pid:
                pos_upd.append((pid, carry, alloc, gross_cbr if kind != "HTM" and ccy != "RUB" else None))

        pos = con.execute("SELECT * FROM position WHERE run_id=? AND instr_type='BOND' AND ocp_transfer=0 ORDER BY rowid", (rid,)).fetchall()
        for p in pos:
            b = books.get(p["book_id"]) or {}
            if p["position_id"] in covered:
                continue
            if "HTM" in kinds and (b.get("book_type") == "HTM" or p["accrual_only"]):
                if p["purchase_price"] is None or p["contract_rate"] is None:
                    problems.append(f"HTM {p['position_id']}: нет purchase_price или contract_rate")
                    continue
                add("HTM", p["book_id"], p["position_id"], p["ccy"], -p["notional"] * p["purchase_price"] / 100,
                    {"fixed_rate": p["contract_rate"]}, 365, True,
                    {"deal_id": f"HTM|{p['position_id']}", "type": "FTP HTM (контракт)", "rate_expr": f"{p['contract_rate']:.2f}% фикс до погашения",
                     "benchmark": "КС", "start_date": p["purchase_date"], "maturity": p["maturity"]})
            elif "POS" in kinds and b.get("book_type") == "TRADING":
                r = resolve_funding_rule(rules, b, p["ccy"], dict(p))
                if not r:
                    problems.append(f"POS {p['position_id']}: нет правила ставки фондирования ({p['book_id']}, {p['ccy']})")
                    continue
                add("POS", p["book_id"], p["position_id"], p["ccy"], -(p["mv"] - p["notional"] * (p["accrued"] or 0) / 100), r, 365, True,
                    {"deal_id": f"POS|{p['position_id']}", "type": "FTP торговой книги", "benchmark": BENCH_POS.get(p["ccy"])})
        if "CASH" in kinds and "CASH" not in kinds_have:
            snap = con.execute("SELECT MAX(as_of) FROM cash_snapshot WHERE as_of <= ?", (d,)).fetchone()[0]
            for c in con.execute("SELECT * FROM cash_snapshot WHERE as_of=? ORDER BY book_id, ccy", (snap,)).fetchall() if snap else []:
                b = books.get(c["book_id"])
                if not b:
                    problems.append(f"CASH: книги {c['book_id']} нет в прогоне")
                    continue
                r = resolve_funding_rule(rules, b, c["ccy"]) or {"index_name": ON_INDEX.get(c["ccy"]), "spread_bp": 0}
                add("CASH", c["book_id"], None, c["ccy"], c["balance"], r, DAY_BASIS.get(c["ccy"], 365), False,
                    {"deal_id": f"CASH|{c['book_id']}|{c['ccy']}", "type": "Остаток ДС", "benchmark": r.get("index_name")})
        if "REPO" in kinds and "REPO" not in kinds_have:
            for f in con.execute("SELECT * FROM fund_snapshot WHERE as_of=? ORDER BY book_id, deal_id", (d,)).fetchall():
                if f["deal_id"] in have_ids or f["book_id"] not in books:
                    continue
                r = {"index_name": f["index_name"], "spread_bp": f["spread_bp"] or 0} if f["index_name"] else {"fixed_rate": f["rate"]}
                if r.get("index_name") and daily(r["index_name"], 0, None) is None:
                    r = {"fixed_rate": f["rate"]}
                add("REPO", f["book_id"], None, f["ccy"], f["amount"], r, DAY_BASIS.get(f["ccy"], 365), False,
                    {"deal_id": f["deal_id"], "type": f["type"], "benchmark": "КС" if f["ccy"] == "RUB" else ON_INDEX.get(f["ccy"]),
                     "start_date": f["start_date"] or d, "maturity": f["maturity"], "counterparty": f["counterparty_id"],
                     "collateral": f["collateral"]})
        self.add_funding(lines)
        with self.con:
            for pid, carry, alloc, alloc_cbr in pos_upd:
                n = self.con.execute("UPDATE position_pnl SET funding=?, fx_funding=?, fx_funding_cbr=? WHERE run_id=? AND position_id=?",
                                     (carry, alloc, alloc_cbr, rid, pid)).rowcount
                if not n:
                    self.con.execute("INSERT INTO position_pnl (run_id, position_id, funding, fx_funding, fx_funding_cbr) VALUES (?,?,?,?,?)",
                                     (rid, pid, carry, alloc, alloc_cbr))
        for m in problems:
            self.log("WARN", f"фондирование: {m}")
        out = {}
        for x in lines:
            out[x["kind"]] = out.get(x["kind"], 0) + 1
        if lines:
            self.log("INFO", "строки фондирования построены: " + ", ".join(f"{k} {v}" for k, v in out.items()))
        return out

    def add_ocp_transfers(self, transfers=None):
        """Сводка вечерней передачи ОВП книг TRADING в книги ocp_to (§0.4): [{from, to, ccy, amount, before?, rate?, value_date?}].
        Без аргумента — строится из прогона: before = FX-дельты бумаг книги + тела строк фондирования в валюте,
        amount = −Σ notional сделок передачи (ocp_transfer = 1) книги в этой валюте, rate — курс ЦБ на завтра."""
        if transfers is None:
            transfers = self._ocp_summary()
        with self.con:
            _ins(self.con, "run_ocp_transfer", ["run_id", "ord", "from_book", "to_book", "ccy", "amount", "before", "rate", "value_date"],
                 [(self.run_id, i, t.get("from") or t.get("from_book"), t.get("to") or t.get("to_book"), t["ccy"], t["amount"], t.get("before"),
                   t.get("rate"), t.get("value_date")) for i, t in enumerate(transfers)])
        return transfers

    def _ocp_summary(self):
        from . import market as M
        con, rid = self.con, self.run_id
        nxt = M.next_business_day(con, self.as_of)
        nxt_rate = {r[0]: r[1] or r[2] for r in con.execute("SELECT ccy, rate_cbr_next, rate FROM mkt_fx WHERE run_id=?", (rid,))}
        before, moved = _book_ocp(con, rid, exclude_transfers=True), {}
        for r in con.execute("SELECT p.book_id, x.ccy, SUM(x.delta) FROM position p JOIN position_fx x ON x.run_id=p.run_id AND x.position_id=p.position_id"
                             " WHERE p.run_id=? AND p.ocp_transfer=1 GROUP BY p.book_id, x.ccy", (rid,)):
            moved[(r[0], r[1])] = -r[2]
        out = []
        for b in con.execute("SELECT book_id, ocp_to FROM run_book WHERE run_id=? AND book_type='TRADING' AND ocp_to IS NOT NULL ORDER BY ord", (rid,)):
            for (bk, ccy), amt in moved.items():
                if bk == b[0] and amt:
                    out.append({"from": bk, "to": b[1], "ccy": ccy, "amount": amt, "before": before.get((bk, ccy), 0.0),
                                "rate": nxt_rate.get(ccy), "value_date": nxt})
        return out

    # ------------------------------------------------------------------ сценарии
    def add_scenario_results(self, scenario_id, positions, funding_residual=None):
        """Полная переоценка сценария. positions: [{position_id, rates, spread, fx_gross, fx_funding, convexity, total}];
        funding_residual: {book_id: RUB} — FX остатков ДС и РЕПО книги (строки без position_id)."""
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
        curves = {r[0] for r in q("SELECT curve_id FROM run_curve WHERE run_id=?")}
        bks = {r[0] for r in con.execute("SELECT bucket FROM ref_bucket")}
        for (f,) in q("SELECT factor_1 FROM position_gamma WHERE run_id=? UNION SELECT factor_2 FROM position_gamma WHERE run_id=?", rid):
            if not GAMMA_FACTOR.match(f):
                err.append(f"гамма: неизвестный фактор '{f}' (ожидается IR|<кривая>|<бакет>, CS или FX|<валюта>)")
            elif f.startswith("IR|") and (f.split("|")[1] not in curves or f.split("|")[2] not in bks):
                err.append(f"гамма: фактор '{f}' — нет такой кривой в прогоне или бакета")
            elif f.startswith("FX|") and f[3:] not in have:
                err.append(f"гамма: фактор '{f}' — нет курса валюты")
        for r in q("SELECT position_id, spread_type FROM position WHERE run_id=? AND spread_type IS NOT NULL"):
            if r[1] not in SPREAD_TYPES:
                err.append(f"позиция {r[0]}: spread_type='{r[1]}', ожидается одно из {', '.join(SPREAD_TYPES)}")
        for r in q("SELECT position_id, yield_date FROM position WHERE run_id=? AND yield_date IS NOT NULL"):
            if not _DATE.match(str(r[1])):
                err.append(f"позиция {r[0]}: yield_date='{r[1]}', ожидается YYYY-MM-DD")
        # 1.3: книги, строки фондирования, HTM
        books = {r[0]: r for r in q("SELECT book_id, book_type, ocp_to FROM run_book WHERE run_id=?")}
        for b, r in books.items():
            if r[1] is not None and r[1] not in BOOK_TYPES:
                err.append(f"книга {b}: book_type='{r[1]}', ожидается одно из {', '.join(BOOK_TYPES)}")
            if r[2] is not None and r[2] not in books:
                err.append(f"книга {b}: ocp_to='{r[2]}' — нет такой книги в прогоне")
        for r in q("SELECT deal_id, kind FROM funding_deal WHERE run_id=? AND kind NOT IN ('POS','HTM','CASH','REPO','DEAL')"):
            err.append(f"фондирование {r[0]}: kind='{r[1]}', ожидается POS / HTM / CASH / REPO")
        pos_book = {r[0]: (r[1], books.get(r[1], (None, None))[1]) for r in q("SELECT position_id, book_id FROM position WHERE run_id=?")}
        for r in q("SELECT deal_id, kind, position_id, book_id FROM funding_deal WHERE run_id=? AND kind IN ('POS','HTM')"):
            need = "TRADING" if r[1] == "POS" else "HTM"
            if r[2] not in pos_book:
                err.append(f"фондирование {r[0]} ({r[1]}): нет позиции position_id='{r[2]}'")
            elif pos_book[r[2]][1] is not None and pos_book[r[2]][1] != need:
                err.append(f"фондирование {r[0]} ({r[1]}): позиция {r[2]} в книге {pos_book[r[2]][0]} режима {pos_book[r[2]][1]}, ожидается {need}")
            elif pos_book[r[2]][0] != r[3]:
                self.log("WARN", f"фондирование {r[0]}: книга строки {r[3]} ≠ книга позиции {pos_book[r[2]][0]}")
        for r in q("SELECT p.position_id, b.book_id FROM position p JOIN run_book b ON b.run_id=p.run_id AND b.book_id=p.book_id"
                   " WHERE p.run_id=? AND b.book_type='HTM' AND (EXISTS (SELECT 1 FROM position_risk x WHERE x.run_id=p.run_id AND x.position_id=p.position_id)"
                   " OR EXISTS (SELECT 1 FROM position_gamma g WHERE g.run_id=p.run_id AND g.position_id=p.position_id))"):
            err.append(f"позиция {r[0]}: книга HTM {r[1]} — position_risk и position_gamma должны быть пусты")
        # предупреждения
        if _ver(self.contract_version) >= (1, 3):
            for b, r in books.items():
                if r[1] is None:
                    self.log("WARN", f"книга {b}: не задан book_type (TRADING / HTM / DERIVS)")
        for r in q("SELECT position_id, spread_curve FROM position WHERE run_id=? AND spread_curve IS NOT NULL"
                   " AND spread_curve NOT IN (SELECT curve_id FROM run_curve WHERE run_id=?)", rid):
            self.log("WARN", f"позиция {r[0]}: spread_curve '{r[1]}' нет среди кривых прогона")
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
            self.log("WARN", f"{n_noff} позиций без официального PnL (ожидается T+1: attach_official_pnl)")
        for r in q("SELECT d.book_id, SUM(f.fx_allocated), (SELECT COALESCE(SUM(x.fx_funding),0) FROM position_pnl x JOIN position p"
                   " ON p.run_id=x.run_id AND p.position_id=x.position_id WHERE x.run_id=d.run_id AND p.book_id=d.book_id)"
                   " FROM funding_deal d JOIN funding_pnl f ON f.run_id=d.run_id AND f.deal_id=d.deal_id WHERE d.run_id=? GROUP BY d.book_id"):
            if abs((r[1] or 0) - (r[2] or 0)) > max(1000.0, 1e-3 * abs(r[1] or 0)):
                self.log("WARN", f"книга {r[0]}: funding fx_allocated={r[1]:.0f}, сумма position fx_funding={r[2]:.0f}")
        for r in q("SELECT d.position_id, SUM(f.fx_allocated), COALESCE(MAX(x.fx_funding), 0) FROM funding_deal d"
                   " JOIN funding_pnl f ON f.run_id=d.run_id AND f.deal_id=d.deal_id"
                   " LEFT JOIN position_pnl x ON x.run_id=d.run_id AND x.position_id=d.position_id"
                   " WHERE d.run_id=? AND d.position_id IS NOT NULL GROUP BY d.position_id"):
            if abs((r[1] or 0) - (r[2] or 0)) > FX_FUNDING_TOL:
                self.log("WARN", f"позиция {r[0]}: fx_funding={r[2]:.0f} ≠ fx_allocated её строки фондирования {r[1]:.0f}")
        fxr = {r[0]: r[1] for r in q("SELECT ccy, rate FROM mkt_fx WHERE run_id=?")}
        for (b, ccy), v in sorted(_book_ocp(con, rid).items()):
            if books.get(b, (None, None))[1] == "TRADING" and ccy != "RUB" and abs(v * fxr.get(ccy, 0)) > OCP_WARN_RUB:
                self.log("WARN", f"книга {b}: ОВП {ccy} после передачи {v:,.0f} (≈{v * fxr.get(ccy, 0) / 1e6:,.1f} млн ₽) — больше порога")
        n_otc = con.execute("SELECT COUNT(*) FROM ref_otc_deal WHERE purpose='OCP_TRANSFER' AND trade_date=?", (self.as_of,)).fetchone()[0]
        if n_otc and not q("SELECT 1 FROM position WHERE run_id=? AND ocp_transfer=1 LIMIT 1"):
            self.log("WARN", f"передача ОВП проведена ({n_otc} сделок в otc_deals), но в прогоне нет сделок передачи (ocp_transfer)")
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
                self.con.execute("UPDATE calc_run SET official_at=? WHERE run_id=?", (now_iso(), self.run_id))
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
def _book_ocp(con, run_id, exclude_transfers=False):
    """{(book_id, ccy): ОВП в валюте} = FX-дельты позиций + тела строк фондирования (кроме HTM)."""
    out = {}
    for r in con.execute("SELECT p.book_id, x.ccy, SUM(x.delta) FROM position p JOIN position_fx x ON x.run_id=p.run_id AND x.position_id=p.position_id"
                         " WHERE p.run_id=?" + (" AND p.ocp_transfer=0" if exclude_transfers else "") + " GROUP BY p.book_id, x.ccy", (run_id,)):
        out[(r[0], r[1])] = out.get((r[0], r[1]), 0.0) + r[2]
    for r in con.execute("SELECT book_id, ccy, SUM(amount) FROM funding_deal WHERE run_id=? AND ccy<>'RUB' AND kind<>'HTM' GROUP BY book_id, ccy", (run_id,)):
        out[(r[0], r[1])] = out.get((r[0], r[1]), 0.0) + r[2]
    return out


EXPLAINED_SQL = "x.carry+x.rolldown+x.rates+x.spread+x.fx_gross+x.fx_funding+x.funding+x.convexity+x.new_trades+x.other"
FX_BASIS_SQL = "(COALESCE(x.fx_gross_cbr, x.fx_gross) + COALESCE(x.fx_funding_cbr, x.fx_funding) - x.fx_gross - x.fx_funding)"


def run_totals(con, run_id):
    """Итоги деска по прогону: (pnl_official, pnl_explained, dv01, cs01).
    Официальный — по курсам ЦБ (позиции без official — их explained_cbr); explained — флэш по рыночным курсам.
    Фондирование облигаций уже в PnL бумаг (funding, fx_funding), поэтому из строк фондирования добавляются
    только строки без position_id (остатки ДС, РЕПО, сделки 1.2): кэрри + FX-переоценка (официальный — по курсам ЦБ)."""
    p = con.execute(
        f"SELECT COALESCE(SUM(p.dv01),0), COALESCE(SUM(p.cs01),0), COALESCE(SUM({EXPLAINED_SQL}),0),"
        f" COALESCE(SUM(COALESCE(x.official, {EXPLAINED_SQL} + {FX_BASIS_SQL})),0)"
        " FROM position p LEFT JOIN position_pnl x ON x.run_id=p.run_id AND x.position_id=p.position_id WHERE p.run_id=?",
        (run_id,)).fetchone()
    f = con.execute("SELECT COALESCE(SUM(f.carry + f.fx_gross - f.fx_allocated),0), COALESCE(SUM(f.carry + COALESCE(f.fx_gross_cbr, f.fx_gross) - f.fx_allocated),0)"
                    " FROM funding_pnl f JOIN funding_deal d ON d.run_id=f.run_id AND d.deal_id=f.deal_id WHERE f.run_id=? AND d.position_id IS NULL",
                    (run_id,)).fetchone()
    return p[3] + f[1], p[2] + f[0], p[0], p[1]


def _store_history(con, run_id, as_of):
    off, exp, dv, cs = run_totals(con, run_id)
    con.execute("INSERT INTO desk_history (as_of, run_id, pnl_official, pnl_explained, dv01, cs01, updated_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(as_of) DO UPDATE SET run_id=excluded.run_id, pnl_official=excluded.pnl_official,"
                " pnl_explained=excluded.pnl_explained, dv01=excluded.dv01, cs01=excluded.cs01, updated_at=excluded.updated_at",
                (as_of, run_id, off, exp, dv, cs, now_iso()))


def upsert_scenarios(db_path, scenarios):
    """[{scenario_id, name, definition: {curves?, fx?, ofzSpread?, spreads?}, active?}] — порядок списка = порядок в UI."""
    con = init_db(db_path)
    with con:
        for i, s in enumerate(scenarios):
            con.execute("INSERT INTO scenario_def (scenario_id, name, ord, definition_json, active, updated_at) VALUES (?,?,?,?,?,?)"
                        " ON CONFLICT(scenario_id) DO UPDATE SET name=excluded.name, ord=excluded.ord,"
                        " definition_json=excluded.definition_json, active=excluded.active, updated_at=excluded.updated_at",
                        (s["scenario_id"], s["name"], s.get("ord", i), json.dumps(s["definition"], ensure_ascii=False),
                         1 if s.get("active", True) else 0, now_iso()))
    con.close()


def upsert_carry_scenarios(db_path, scenarios, replace=False):
    """Сценарии кэрри и фондирования (§0.8, docs/SCENARIOS.md §7), глобально:
    [{scenario_id, name, definition: {ks: {mode, steps?, path?}, on?, fundSpread?, hardCcy?}, active?}].
    Принимается и форма window.DESK: {id, name, ks, on?, ...}. Порядок списка = порядок в UI; первый — база сравнения.
    replace=True — сценарии, которых нет в списке, выключаются."""
    meta = ("scenario_id", "id", "name", "ord", "active", "definition")
    con = init_db(db_path)
    try:
        with con:
            if replace:
                con.execute("UPDATE carry_scenario_def SET active=0")
            for i, s in enumerate(scenarios):
                sid = s.get("scenario_id") or s["id"]
                d = s.get("definition") or {k: v for k, v in s.items() if k not in meta}
                if not isinstance(d.get("ks", {}), dict) or d.get("ks", {}).get("mode", "flat") not in ("flat", "implied", "steps", "path"):
                    raise ValueError(f"сценарий кэрри {sid}: ks.mode — flat / implied / steps / path")
                con.execute("INSERT INTO carry_scenario_def (scenario_id, name, ord, definition_json, active, updated_at) VALUES (?,?,?,?,?,?)"
                            " ON CONFLICT(scenario_id) DO UPDATE SET name=excluded.name, ord=excluded.ord,"
                            " definition_json=excluded.definition_json, active=excluded.active, updated_at=excluded.updated_at",
                            (sid, s["name"], s.get("ord", i), json.dumps(d, ensure_ascii=False), 1 if s.get("active", True) else 0, now_iso()))
    finally:
        con.close()


def upsert_cbr_meetings(db_path, meetings):
    """Заседания ЦБ по КС: [{date, effective_date?}] (или effective). Пустая effective_date — следующий рабочий день."""
    con = init_db(db_path)
    try:
        with con:
            con.executemany("INSERT INTO ref_cbr_meeting (date, effective_date, updated_at) VALUES (?,?,?)"
                            " ON CONFLICT(date) DO UPDATE SET effective_date=excluded.effective_date, updated_at=excluded.updated_at",
                            [(m["date"], m.get("effective_date") or m.get("effective"), now_iso()) for m in meetings])
    finally:
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


def attach_official_pnl(db_path, run_id=None, as_of=None, overwrite=True):
    """T+1: приложить официальный PnL (pnl_official за дату прогона, по position_id) к УЖЕ опубликованному EOD-прогону.
    Новый прогон не создаётся: меняются только position_pnl.official, calc_run.official_at и строка desk_history.
    run_id — прогон; иначе последний опубликованный EOD за as_of. overwrite=False — не трогать уже заполненный official.
    Возвращает {run_id, as_of, updated, missing} (missing — позиции pnl_official, которых нет в прогоне)."""
    con = init_db(db_path)
    try:
        if run_id:
            run = con.execute("SELECT * FROM calc_run WHERE run_id=? AND status='complete'", (run_id,)).fetchone()
        elif as_of:
            run = con.execute("SELECT * FROM calc_run WHERE as_of=? AND run_type='EOD' AND status='complete'"
                              " ORDER BY published_at DESC, run_id DESC LIMIT 1", (as_of,)).fetchone()
        else:
            raise ValueError("нужен run_id или as_of")
        if not run:
            raise ValueError(f"опубликованный прогон не найден ({'run_id=' + str(run_id) if run_id else 'EOD за ' + as_of})")
        rid, d = run["run_id"], run["as_of"]
        off = {r[0]: r[1] for r in con.execute("SELECT position_id, pnl_rub FROM pnl_official WHERE as_of=? AND position_id<>''", (d,))}
        if not off:
            raise ValueError(f"нет официального PnL за {d} по позициям (pnl_official.csv)")
        pos = {r[0] for r in con.execute("SELECT position_id FROM position WHERE run_id=?", (rid,))}
        have = {r[0]: r[1] for r in con.execute("SELECT position_id, official FROM position_pnl WHERE run_id=?", (rid,))}
        upd = [(v, rid, k) for k, v in off.items() if k in have and (overwrite or have[k] is None)]
        ins = [(rid, k, v) for k, v in off.items() if k in pos and k not in have]
        missing = sorted(k for k in off if k not in pos)
        with con:
            con.executemany("UPDATE position_pnl SET official=? WHERE run_id=? AND position_id=?", upd)
            _ins(con, "position_pnl", ["run_id", "position_id", "official"], ins)
            con.execute("UPDATE calc_run SET official_at=? WHERE run_id=?", (now_iso(), rid))
            h = con.execute("SELECT run_id FROM desk_history WHERE as_of=?", (d,)).fetchone()
            if run["run_type"] == "EOD" and (h is None or h[0] in (None, rid)):
                _store_history(con, rid, d)
            con.execute("INSERT INTO calc_run_log (run_id, ts, level, message) VALUES (?,?,?,?)",
                        (rid, now_iso(), "INFO", f"приложен официальный PnL за {d}: {len(upd) + len(ins)} позиций"
                         + (f"; нет в прогоне: {len(missing)}" if missing else "")))
        return {"run_id": rid, "as_of": d, "updated": len(upd) + len(ins), "missing": missing}
    finally:
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
