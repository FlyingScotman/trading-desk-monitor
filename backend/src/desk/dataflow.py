"""The data flow of a business date (TDM#586): the monitors' screen C21 «Схема данных», a block diagram of where the
data of the date came from, what was computed of it, where it stopped and why, and what comes next.

``build(con, settings, on, autopilot=..., services=..., admin=...)`` is ``GET /api/v1/dataflow?on=``. It reads only
what the backend already has and never asks the hub or the adapter's services: the day status (``daystatus.day``: the
inputs by dataset, the files and their rejections, the hub's closes in the cache, the runs, where the positions went,
the services' feeds), the day's trades (``trades.brief``), the date's default run (its lineage: the trades taken into
the day's positions, the OCP and its gaps; its positions: valued, with a PnL, why not), the history of runs
(``backfill.status``), the hub's history in the store (``hubhistory.status``), the trades of past dates
(``AdapterServices.history``) and the autopilot's step now (``Autopilot.status``). A part that cannot be read is said
in its block (status ``error``, in words: ``FAILED``), the rest is still built. The screen asks every minute per tab:
the history of runs (``backfill.status``, a query per date of the history) is kept ``BACKFILL_TTL_S`` seconds.

**Blocks** in three columns: ``source`` (reports, the services' feeds, the hub, reference data), ``process`` (reading,
storing, the day's trades into the positions, the core's valuation, the OCP, the PnL, the history of runs, the history
loads) and ``result`` (the screens). Each: ``status`` (``STATUS``: готово / идёт / ждёт / не пришло / ошибка /
выключено; a result takes the worst of its input's waiting and error, ``RESULT_TAKES``) and its
words, ``got`` (what was received and when), ``dropped`` (what was left out and why), ``waits`` (what it waits for),
``progress`` (``done`` of ``total`` of a background work), ``link`` (the screen with the details) and ``fix`` (the
setting that turns it on or fixes it: only for a user who may open the settings, ``admin``). A line is ``{t, at, tone}``:
the text, an optional time (ISO, the page shows it on the Moscow clock) and ``bad`` / ``warn`` / none.

**Edges** ``{from, to, what, incomplete, why}``: which data goes where; ``incomplete`` when data went on although a part
of it was missing (``why``: «ОВП посчитана без остатков на счетах»): the page draws it red. Into the valuation and what
follows it only when the date has a run: without one nothing went on.

A source of a past date that is never awaited later (the day's trades, the official PnL, the trade terms) is «не пришло»
(``missing``), not «ждёт», and is not among the reports awaited ahead; a stock source (the OCP report, the balances,
the trade terms) whose latest part in force is older than the date waits for the date's report while the parts of
past dates stay in force.

**Ahead** (``ahead``): the queue of background work (dates of the history to compute, trades of past dates to fetch,
hub history to load, reports still awaited, the next feed) with what is known of the time left (``eta_s``).

Codes, counts, book ids, file names and sources, as the day status shows every signed-in user (#25); no values of
positions, no PnL figures, no folder paths.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg

from . import daystatus, store, trades
from .settings import Settings

log = logging.getLogger(__name__)

STATUS = {"ok": "готово", "running": "идёт", "waiting": "ждёт", "missing": "не пришло", "error": "ошибка",
          "off": "выключено"}
"""A block's status and its word on the screen."""
ORDER = ("ok", "off", "missing", "waiting", "running", "error")
"""Worse to the right: a block keeps the worst status said of it (``_status``)."""
RESULT_TAKES = ("waiting", "error")
"""The statuses of its input a result block takes (its one edge in): a screen shows what its step got."""
PAST_MISSING = ("trades", "pnl_official", "trade_terms")
"""Sources not awaited after their date: on a past date without them, «не пришло»."""
BACKFILL_TTL_S = 60.0
"""How long the history of runs (``backfill.status``) of a store is kept for the screen (``_backfill``)."""
FAILED = {"OperationalError": "нет связи с хранилищем", "InterfaceError": "нет связи с хранилищем",
          "QueryCanceled": "хранилище не ответило вовремя", "TimeoutError": "не ответило вовремя",
          "OSError": "файл не прочитан", "FileNotFoundError": "файл не найден", "PermissionError": "нет доступа к файлу"}
"""A part not read, in words (``Flow.failed``); the exception's name only in a secondary line."""
ADAPTER_ERROR = {"config_dir_missing": "не найдена папка настроек адаптера",
                 "not_found": "пакет адаптера не найден",
                 "fingerprint_mismatch": "файлы адаптера отличаются от проверенных (задан отпечаток адаптера)",
                 "import_error": "код адаптера не загружается",
                 "unsupported_contract": "адаптер написан для более новой версии программы",
                 "unsupported_spi": "адаптер написан для более новой версии программы",
                 "bad_source": "адаптер объявляет данные, которых эта версия программы не знает",
                 "bad_describe": "адаптер описывает себя не по договору"}
"""Why the adapter did not load (``AdapterHandle.error`` code), in words."""
COLUMNS = [{"id": "source", "title": "Откуда приходят данные"}, {"id": "process", "title": "Что с ними делается"},
           {"id": "result", "title": "Где это видно"}]
STEP_BLOCK = {"sync": "market", "scan": "read", "eod": "value", "intraday": "value", "morning": "value",
              "history": "history_load", "backfill": "backfill"}
"""The autopilot's step now -> the block that is ``running`` meanwhile."""
FEED_STATE = {"complete": "получено", "partial": "получено частично", "skipped": "нечего загружать",
              "blocked": "остановлено", "error": "ошибка", "cancelled": "отменено"}
HISTORY_WAIT = {"login_required": "нужен вход в сервисы адаптера", "not_ready": "сервис ещё не готов к работе",
                "settings_required": "нужна настройка сервисов адаптера"}
LOST = ("rejected", "failed", "timed_out", "match_failed")
CASH_GAPS = ("balances_older_than_base", "balances_not_at_base")
OCP_REPORT_GAPS = ("no_report", "stale_report", "base_older", "carried_forward_unit")
WAIT_NOUN = {"ocp": "отчёт ОВП", "cash_balances": "остатки на счетах", "pnl_official": "официальный PnL",
             "trade_terms": "условия сделок"}
"""What a source block waits for (``waits``): «отчёт ОВП за 15.10»."""


def _line(t: str, at: Any = None, tone: str | None = None) -> dict[str, Any]:
    return {"t": t, "at": at.isoformat() if isinstance(at, datetime) else at, "tone": tone}


def _dm(d: Any) -> str:
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d[:10])
        except ValueError:
            return d
    return f"{d:%d.%m.%Y}" if isinstance(d, date) else "—"


def _n(v: Any) -> str:
    return f"{int(v):,}".replace(",", " ") if isinstance(v, int | float) else "—"


class Flow:
    """The blocks and the edges being built (``build``)."""

    def __init__(self, admin: bool) -> None:
        self.admin = admin
        self.blocks: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.ahead: list[dict[str, Any]] = []

    def block(self, bid: str, column: str, title: str, what: str, link: tuple[str, str] | None = None,
              fix: tuple[str, str] | None = None) -> dict[str, Any]:
        b = {"id": bid, "column": column, "title": title, "what": what, "status": "ok", "got": [], "dropped": [],
             "waits": None, "progress": None, "at": None, "link": {"href": link[0], "text": link[1]} if link else None,
             "fix": {"href": fix[0], "text": fix[1]} if fix and self.admin else None,
             "fix_note": None if fix is None or self.admin else f"настройка «{fix[1]}» — у администратора"}
        self.blocks[bid] = b
        return b

    def edge(self, a: str, b: str, what: str, why: str | None = None) -> None:
        self.edges.append({"from": a, "to": b, "what": what, "incomplete": bool(why), "why": why})

    def failed(self, b: dict[str, Any], e: BaseException) -> None:
        """A part that could not be read: the block says so in words, the rest is built; the exception's name in a
        secondary line, for the administrator."""
        b["status"] = "error"
        name = type(e).__name__
        why = next((FAILED[c.__name__] for c in type(e).__mro__ if c.__name__ in FAILED), "внутренняя ошибка монитора")
        b["dropped"].append(_line(f"Сведения этого шага не прочитаны: {why}. Схема покажет их при следующем обновлении.",
                                  tone="bad"))
        b["dropped"].append(_line(f"для администратора: {name}, подробности — в журнале монитора"))


def _said_error(b: dict[str, Any], what: str, error: Any) -> None:
    """An error another module gave as text («OperationalError: …»): in words (``FAILED``); only the exception's
    name, never its text, in a secondary line for the administrator."""
    name = str(error).split(":", 1)[0].strip()
    b["dropped"].append(_line(f"{what}: {FAILED.get(name, 'внутренняя ошибка монитора')}", tone="bad"))
    if name.isidentifier():
        b["dropped"].append(_line(f"для администратора: {name}, подробности — в журнале монитора"))


def _status(b: dict[str, Any], st: str) -> None:
    if ORDER.index(st) > ORDER.index(b["status"]) or b["status"] == "ok":
        b["status"] = st


def _adapter_error(err: str | None) -> list[dict[str, Any]]:
    """The adapter that did not load, in a sentence, and its code (never the detail: it names settings variables)."""
    code = str(err or "").split(":", 1)[0].strip()
    why = ADAPTER_ERROR.get(code) or ("адаптер не запустился" if code.startswith(("locate_", "describe_"))
                                      else "адаптер не загрузился")
    return [_line(f"Отчёты не читаются: {why}. Проверьте адаптер в настройках («Проверка адаптера»).", tone="bad"),
            _line(f"код причины: {code or '—'}")]


def _latest(*ts: Any) -> str | None:
    vals = [str(t) for t in ts if t]
    return max(vals) if vals else None


def _safe(flow: Flow, b: dict[str, Any], fn: Callable[[], Any], con: psycopg.Connection | None = None) -> Any:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - one block says it, the screen is still built
        log.warning("dataflow: %s not read (%s: %s)", b["id"], type(e).__name__, e)
        if con is not None and isinstance(e, psycopg.Error):
            con.rollback()
        flow.failed(b, e)
        return None


def _declared(settings: Settings) -> dict[str, list[str]]:
    """Per dataset the adapter's sources of it (an adapter that does not load: none)."""
    from .inbox import scan

    out: dict[str, list[str]] = {}
    for src, dss in daystatus._declared(scan.adapter(settings)).items():
        for ds in dss:
            out.setdefault(ds, []).append(src)
    return out


def _run(con: psycopg.Connection, run_id: int | None) -> dict[str, Any] | None:
    if run_id is None:
        return None
    r = con.execute("SELECT run_id, run_type, cut_type, cut_ts, completed_at, lineage, coverage, business_date"
                    " FROM run WHERE run_id = %s", (run_id,)).fetchone()
    if r is None:
        return None
    keys = ("run_id", "run_type", "cut_type", "cut_ts", "completed_at", "lineage", "coverage", "business_date")
    return dict(zip(keys, r, strict=True))


def _positions_facts(con: psycopg.Connection, run_id: int) -> dict[str, Any]:
    """The run's positions: how many, valued, with a PnL, the assumptions, and why not (codes with their words)."""
    from .api.coverage import CAUSES
    from .pnl.reasons import no_pnl_reason_text

    n, valued, with_pnl, assumed = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE quality IS NOT NULL), count(pnl),"
        " count(*) FILTER (WHERE jsonb_typeof(pnl->'extra'->'assumption') = 'object')"
        " FROM run_position WHERE run_id = %s", (run_id,)).fetchone()
    not_valued = Counter()
    for (why,) in con.execute("SELECT reason FROM run_position WHERE run_id = %s AND quality IS NULL", (run_id,)):
        not_valued[str(why or "not_valued").split(":", 1)[0].strip()] += 1
    no_pnl = Counter()
    for (why,) in con.execute("SELECT no_pnl->>'reason' FROM run_position WHERE run_id = %s AND pnl IS NULL"
                              " AND quality IS NOT NULL", (run_id,)):
        no_pnl[why or "not_computed"] += 1
    return {"positions": n, "valued": valued, "with_pnl": with_pnl, "assumed": assumed,
            "not_valued": [{"code": k, "n": v, "text": (CAUSES.get(k) or (k,))[0]} for k, v in not_valued.most_common(5)],
            "not_valued_n": sum(not_valued.values()),
            "no_pnl": [{"code": k, "n": v, "text": no_pnl_reason_text(k)} for k, v in no_pnl.most_common(5)],
            "no_pnl_n": sum(no_pnl.values())}


def _ocp_block(con: psycopg.Connection, run_id: int) -> dict[str, Any] | None:
    row = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'ocp'", (run_id,)).fetchone()
    return row[0] if row else None


def _stock_units(con: psycopg.Connection, dataset: str, d: date) -> list[dict[str, Any]]:
    """The units of a stock dataset in force on ``d`` (the latest valid date <= ``d`` of each)."""
    return store.dataset_rows(con, dataset, d).units if dataset else []


def _prev_business_day(settings: Settings, d: date) -> date:
    """The business day before ``d`` by the stored Russian calendar; Monday..Friday when it is not readable."""
    try:
        from .inbox.static_index import business_calendar  # at call time: the inbox is heavy to import

        return business_calendar(settings.snapshot_cache, d).prev_business_day(d)
    except Exception:  # noqa: BLE001 - no calendar: weekdays only
        p = d - timedelta(days=1)
        while p.weekday() >= 5:
            p -= timedelta(days=1)
        return p


def build(con: psycopg.Connection, settings: Settings, on: date | None = None, *,
          autopilot: dict[str, Any] | None = None, services: Any = None, admin: bool = False,
          now: datetime | None = None) -> dict[str, Any]:
    """``GET /api/v1/dataflow`` (module doc). ``on``: the business date, default as the day status's (the date of the
    latest default run, else today, Moscow); ``autopilot``: ``Autopilot.status()`` (None: not running); ``services``:
    the adapter's services (``AdapterServices``, for the trades of past dates; None: not asked)."""
    now = now or datetime.now(UTC)
    today = now.astimezone(store.MSK).date()
    flow = Flow(admin)
    day = daystatus.day(con, settings, on)
    d = date.fromisoformat(day["date"])
    dd = f"{d:%d.%m}"
    summary = day["summary"]
    run_id = summary.get("default_run_id") or summary.get("current_run_id")
    run = _run(con, run_id)
    lineage = (run or {}).get("lineage") or {}
    inputs = {i["dataset"]: i for i in day["inputs"]}
    files_of: dict[str, list[dict[str, Any]]] = {}
    for f in day["files"]:
        for ds in f["datasets"] or ["?"]:
            files_of.setdefault(ds, []).append(f)
    try:
        declared = _declared(settings)
    except Exception:  # noqa: BLE001 - no adapter: every source reads «не настроено»
        declared = {}
    past = d < today
    prev_bd = _prev_business_day(settings, d)
    ap = autopilot or {}
    prog = ap.get("progress") if ap.get("state") == "running" else None
    running_block = STEP_BLOCK.get((prog or {}).get("step"))
    feeds = day.get("feeds") or []
    page_q = f"?on={d.isoformat()}"
    fixes = {"folders": ("../settings.html#adapter/folders", "Папки с отчётами"),
             "services": ("../settings.html#adapter/services", "Сервисы адаптера"),
             "hub": ("../settings.html#hub", "Маркет-дата сервер"),
             "check": ("../settings.html#adapter/check", "Проверка адаптера"),
             "ovp": _ovp_fix(settings),
             "books": ("../settings.html#books", "Книги"),
             "bf": ("../settings.html#bf", "История прогонов"),
             "auto": ("../settings.html#auto", "Автопилот"),
             "indexes": ("../settings.html#indexes", "Индексы ставок")}

    # ---------------------------------------------------------------- sources
    def report_files(b: dict[str, Any], ds: str) -> None:
        for f in files_of.get(ds, [])[:4]:
            tone = "bad" if f["status"] in LOST else "warn" if f["status"] == "partially_accepted" else None
            b["got"].append(_line(f"{f['name']} — {f['status_text']}", f["at"], tone))
            b["at"] = _latest(b["at"], f["at"])
            if f.get("why"):
                b["dropped"].append(_line(f"{f['name']}: {f['why']}", tone="bad"))
            for u in f["units"]:
                if u["status"] == "rejected" and u["dataset"] == ds:
                    sc = ", ".join(str(v) for v in (u["scope"] or {}).values()) or "файл"
                    b["dropped"].append(_line(f"{sc}: {u['why'] or 'отклонено'}", tone="bad"))
        more = len(files_of.get(ds, [])) - 4
        if more > 0:
            b["got"].append(_line(f"и ещё файлов: {more}"))

    # positions
    b = flow.block("positions", "source", "Отчёт о позициях", "позиции книг из отчётов в папках",
                   (f"c14-day-status.html{page_q}", "Статус дня"), fixes["folders"])
    books = (day.get("positions") or {}).get("books") or []
    of_date = [x for x in books if x.get("valid_date") == d.isoformat()]
    older = [x for x in books if x.get("valid_date") and x["valid_date"] < d.isoformat()]
    none = [x for x in books if not x.get("valid_date") and x.get("active", True)]
    report_files(b, "positions")
    inp = inputs.get("positions") or {}
    b["got"].insert(0, _line(f"за {dd}: книг {len(of_date)} из {len(books)}, строк принято "
                             f"{_n((inp.get('rows') or {}).get('accepted', 0))}"))
    pos_why = None
    if older:
        b["dropped"].append(_line(f"книг без отчёта за {dd}: {len(older)} — их позиции с прошлых дат "
                                  f"({', '.join(x['book_id'] for x in older[:5])}{', …' if len(older) > 5 else ''})",
                                  tone="warn"))
        pos_why = f"позиции {len(older)} книг — с прошлых дат"
    if none:
        b["dropped"].append(_line(f"книг без позиций вовсе: {len(none)}", tone="warn"))
    if inp.get("state") == "rejected":
        _status(b, "error")
        pos_why = "отчёт о позициях за дату отклонён"
    elif inp.get("state") == "partial":
        pos_why = pos_why or "отчёт о позициях принят частично"
    if not books or not (of_date or older):
        _status(b, "waiting" if not files_of.get("positions") else "error")
        b["waits"] = f"отчёт о позициях за {dd}"
    elif not of_date:
        _status(b, "waiting")
        b["waits"] = f"отчёт о позициях за {dd}; пока считаются позиции с прошлых дат"

    def not_come(b: dict[str, Any], ds: str, waits: str) -> None:
        """A source without its data of the date: waits for it, or on a past date «не пришло» (``PAST_MISSING``)."""
        if past and ds in PAST_MISSING:
            _status(b, "missing")
            b["waits"] = None
            b["dropped"].insert(0, _line(f"не пришло: {waits}", tone="warn"))
        else:
            _status(b, "waiting")
            b["waits"] = waits

    # stocks and flows of the adapter
    def dataset_block(bid: str, ds: str, title: str, what: str, link: tuple[str, str], fix: tuple[str, str],
                      stock: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        b = flow.block(bid, "source", title, what, link, fix)
        inp = inputs.get(ds) or {}
        units = _safe(flow, b, lambda: _stock_units(con, ds, d), con) if stock else None
        units = units or []
        report_files(b, ds)
        given = bool(declared.get(ds)) or any(x.get("dataset") == ds for x in feeds)
        noun = WAIT_NOUN.get(ds, ds)
        if stock:
            vd = max(u["valid_date"] for u in units) if units else None
            if units:
                b["got"].insert(0, _line(f"в силе: частей {len(units)}, последняя за {_dm(vd)}"))
            if not units and not given and not files_of.get(ds):
                _status(b, "off")
                b["waits"] = None
                b["got"].insert(0, _line("адаптер этот отчёт не даёт"))
            elif not units:
                not_come(b, ds, f"{noun} за {dd}")
            elif given and _as_date(vd) < prev_bd:
                # the parts of past dates stay in force; a report of the previous business day is the normal base
                # (TDM#531 report_base_plus_day, 08 §6.7): only an older last part means the date's report is awaited
                not_come(b, ds, f"{noun} за {dd}; пока в силе части с прошлых дат (последняя за {_dm(vd)})")
        else:
            rows = (inp.get("rows") or {}).get("accepted", 0)
            if inp.get("state") not in (None, "missing"):
                b["got"].insert(0, _line(f"за {dd}: строк принято {_n(rows)}"))
            elif not given:
                _status(b, "off")
                b["got"].insert(0, _line("адаптер этот отчёт не даёт"))
            else:
                not_come(b, ds, f"{noun} за {dd}")
        if inp.get("state") == "rejected":
            _status(b, "error")
        return b, units

    dataset_block("ocp_report", "ocp", "Отчёт ОВП", "ОВП юрлиц из отчёта", ("c16-ocp.html", "ОВП"),
                                       fixes["folders"], True)
    _, cash_units = dataset_block("cash", "cash_balances", "Остатки на счетах",
                                         "остатки по счетам книг: для ОВП и сверки", ("c15-cash.html", "Остатки"),
                                         fixes["folders"], True)
    dataset_block("pnl_official", "pnl_official", "Официальный PnL", "PnL из отчёта: для сверки",
                               ("c10-pnl-explain.html", "PnL explain"), fixes["folders"], False)

    # the services' feeds of a dataset
    def feed_lines(b: dict[str, Any], ds: str) -> None:
        for x in [x for x in feeds if x.get("dataset") == ds]:
            st = "нужен вход" if x.get("needs_login") else "выключено" if not x.get("enabled") else \
                FEED_STATE.get(x.get("state") or "", x.get("state") or "ещё не запускалось")
            tone = "warn" if x.get("needs_login") or x.get("state") in ("blocked", "partial") else \
                "bad" if x.get("state") == "error" else None
            b["got"].append(_line(f"фид «{x['title']}»: {st}", x.get("last_finished") or x.get("last_started"), tone))
            b["at"] = _latest(b["at"], x.get("last_finished"))
            if x.get("needs_login"):
                _status(b, "waiting")
                b["waits"] = "вход в сервисы адаптера"
            elif x.get("state") == "error" and x.get("enabled"):
                _status(b, "error")
            if x.get("enabled") and x.get("next_run"):
                flow.ahead.append({"id": f"feed:{x['id']}", "title": f"Фид «{x['title']}»",
                                   "text": "следующий запрос", "at": x["next_run"], "eta_s": None})

    # the day's trades
    tb = flow.block("trades", "source", "Сделки дня", "сделки даты: из фида сервисов, файлом или догрузкой",
                    (f"c20-trades.html?date={d.isoformat()}", "Сделки дня"), fixes["services"])
    tr = _safe(flow, tb, lambda: trades.brief(con, d, None, getattr(settings, "intraday_trades", True), run_id,
                                              history=trades.history_files(settings)), con)
    if tr is not None:
        tb["got"].append(_line(f"за {dd}: сделок {_n(tr['trades'])}, действуют {_n(tr['live'])}, "
                               f"отменены {_n(tr['cancelled'])}"))
        for s in tr["sources"]:
            tb["got"].append(_line(f"{s['text']}: сделок {_n(s['trades'])}", s["last_at"]))
            tb["at"] = _latest(tb["at"], s["last_at"])
    feed_lines(tb, "trades")
    if tr is not None and not tr["trades"] and tb["status"] == "ok":
        if not declared.get("trades") and not any(x.get("dataset") == "trades" for x in feeds):
            _status(tb, "off")
            tb["got"].insert(0, _line("адаптер сделок дня не даёт"))
        else:
            not_come(tb, "trades", f"сделки за {dd}")

    # trade terms
    terms, _ = dataset_block("terms", "trade_terms", "Условия сделок", "условия деривативов: для оценки ядром",
                             (f"c14-day-status.html{page_q}", "Статус дня"), fixes["services"], True)
    feed_lines(terms, "trade_terms")

    # trades of past dates
    hb = flow.block("past_trades", "source", "Сделки прошлых дат", "догрузка сделок за прошлые даты из сервисов",
                    ("c19-pnl-history.html", "PnL за период"), fixes["services"])
    hist = _safe(flow, hb, services.history) if services is not None else None
    hist_why = None
    if hist is None and hb["status"] == "ok":
        _status(hb, "off")
        hb["got"].append(_line("не настроено"))
    elif hist is not None:
        c = hist.get("counts") or {}
        if not hist.get("available"):
            _status(hb, "off")
            hb["got"].append(_line("адаптер догрузку прошлых дат не даёт"))
        elif not hist.get("enabled"):
            _status(hb, "off")
            hb["got"].append(_line("догрузка выключена в настройках сервисов"))
        else:
            got = (c.get("fetched") or 0) + (c.get("no_trades") or 0)
            hb["got"].append(_line(f"дат с {_dm(hist.get('from'))}: получено {got}, без сделок {c.get('no_trades') or 0}, "
                                   f"осталось {c.get('todo') or 0}"))
            hb["progress"] = {"done": got + (c.get("no_books") or 0), "total": hist.get("dates") or 0,
                              "text": "дат обработано"}
            hb["at"] = (hist.get("last") or {}).get("at") if isinstance(hist.get("last"), dict) else None
            bad = (c.get("unavailable") or 0) + (c.get("unmapped") or 0) + (c.get("partial") or 0)
            if bad:
                hb["dropped"].append(_line(f"дат не получено: {bad} (сервис не отдал или книги не сопоставлены)",
                                           tone="warn"))
                hist_why = f"сделки {bad} прошлых дат не получены"
            if hist.get("waiting"):
                _status(hb, "waiting")
                hb["waits"] = HISTORY_WAIT.get(hist["waiting"], hist["waiting"])
            elif hist.get("current"):
                _status(hb, "running")
                hb["got"].append(_line(f"сейчас запрашивается {_dm(hist['current'])}"))
            elif c.get("todo"):
                _status(hb, "waiting")
                hb["waits"] = f"очередь: {c['todo']} дат, не больше {hist.get('limit_per_day')} запросов в день"
                flow.ahead.append({"id": "past_trades", "title": "Догрузка сделок прошлых дат",
                                   "text": f"осталось дат: {c['todo']} (по {hist.get('limit_per_day')} в день)",
                                   "at": hist.get("next_at"), "eta_s": None,
                                   "progress": hb["progress"]})

    # the hub's closes
    mb = flow.block("market", "source", "Закрытия рынка (хаб)", "срезы цен и кривых за дату из кэша хаба",
                    (f"c14-day-status.html{page_q}", "Статус дня"), fixes["hub"])
    m = day.get("market") or {}
    market_why = None
    if m.get("error"):
        _status(mb, "error")
        _said_error(mb, "кэш снимков не прочитан", m["error"])
    for cut, label in (("eod_final", "окончательное закрытие"), ("eod_preliminary", "предварительное закрытие")):
        x = m.get(cut)
        if x:
            mb["got"].append(_line(label + (f", исправлений {x['corrections']}" if x.get("corrections") else ""),
                                   x.get("known_at") or x.get("cut_ts")))
            mb["at"] = _latest(mb["at"], x.get("known_at"))
    if (m.get("intraday") or {}).get("count"):
        mb["got"].append(_line(f"срезов внутри дня: {m['intraday']['count']}", m["intraday"].get("latest_cut_ts")))
    if not m.get("eod_final") and not m.get("error"):
        _status(mb, "waiting")
        mb["waits"] = f"окончательное закрытие хаба за {dd}"
        if run and run.get("cut_type") != "eod_final" and d < today:
            market_why = "оценка не по окончательному закрытию"
    sync = next((x for x in ap.get("steps") or [] if x.get("name") == "sync"), None)
    if sync and sync.get("status") == "error":
        _status(mb, "error")
        mb["dropped"].append(_line(f"загрузка из хаба: {sync.get('detail')}", sync.get("at"), "bad"))

    # the hub's history in the store
    xb = flow.block("hub_history", "source", "История фиксингов (хаб)", "фиксинги индексов и история рынка",
                    ("c09-market.html", "Рынок"), fixes["hub"])
    bf = _safe(flow, xb, lambda: _backfill(con, settings), con)
    fx = (bf or {}).get("fixings_history") or {}
    if fx.get("error"):
        _status(xb, "error")
        _said_error(xb, "история фиксингов не прочитана", fx["error"])
    elif bf is not None:
        if fx.get("rows"):
            xb["got"].append(_line(f"индексов {fx.get('indices')}, значений {_n(fx.get('rows'))}, "
                                   f"с {_dm(fx.get('from'))} по {_dm(fx.get('to'))}", (fx.get("jobs") or {}).get("fetched_at")))
        if not fx.get("loading"):
            _status(xb, "off")
            xb["got"].append(_line("догрузка истории из хаба выключена"))
        elif not fx.get("rows"):
            _status(xb, "waiting")
            xb["waits"] = "первая загрузка истории фиксингов"
        api = fx.get("api") or {}
        if api and not api.get("available") and api.get("reason"):
            xb["dropped"].append(_line(f"история хаба не используется: {api['reason']}", tone="warn"))

    # reference data
    rb = flow.block("reference", "source", "Справочники и политика ОВП", "книги, юрлица, правила фондирования и ОВП",
                    ("c16-ocp.html", "ОВП"), fixes["books"])
    ref = _safe(flow, rb, lambda: _reference(con, d), con)
    ref_why = None
    if ref is not None:
        rb["got"].append(_line(f"книг в силе: {ref['books']}"))
        for k, (n, at) in ref["kinds"].items():
            rb["got"].append(_line(f"{REFERENCE_NAMES.get(k, k)}: записей {n}", at))
            rb["at"] = _latest(rb["at"], at.isoformat() if at else None)
        if not ref["books"]:
            _status(rb, "waiting")
            rb["waits"] = "справочник книг от адаптера"
        if "ocp_policy" not in ref["kinds"]:
            rb["dropped"].append(_line("политики ОВП от адаптера нет: действуют значения по умолчанию", tone="warn"))
    late = [ds for ds in (day["summary"].get("warnings") or []) if "политика ОВП" in ds]
    if late:
        ref_why = "политика ОВП изменилась после расчёта"

    # ---------------------------------------------------------------- processing
    from .inbox import scan

    pr = flow.block("read", "process", "Чтение отчётов адаптером", "адаптер распознаёт и читает файлы",
                    (f"c14-day-status.html{page_q}", "Статус дня"), fixes["check"])
    try:
        h = scan.adapter(settings)
        if not h.ok:
            _status(pr, "error")
            pr["dropped"].extend(_adapter_error(h.error))
    except Exception as e:  # noqa: BLE001
        flow.failed(pr, e)
    st = Counter(f["status"] for f in day["files"])
    if day["files"]:
        pr["got"].append(_line(f"файлов за {dd}: {len(day['files'])} — " + ", ".join(
            f"{daystatus.FILE_STATUS.get(k, k)} {v}" for k, v in st.most_common())))
        pr["at"] = _latest(*(f["at"] for f in day["files"]))
    lost = [f for f in day["files"] if f["status"] in LOST]
    for f in lost[:5]:
        pr["dropped"].append(_line(f"{f['name']}: {f.get('why') or f['status_text']}", f["at"], "bad"))
    if day.get("copies"):
        pr["dropped"].append(_line(f"старых копий под новым именем: {len(day['copies'])}", tone="warn"))
    ls = scan.last_scan
    if ls:
        pr["got"].append(_line("последний просмотр папок", ls.get("at")))
    if not day["files"] and pr["status"] == "ok":
        _status(pr, "waiting")
        pr["waits"] = f"файлы за {dd}"
    read_why = f"файлов с ошибкой: {len(lost)}" if lost else None

    sb = flow.block("store", "process", "Приём в хранилище", "проверка строк и запись позиций по книгам",
                    (f"c14-day-status.html{page_q}", "Статус дня"), fixes["folders"])
    P = day.get("positions") or {}
    t = P.get("totals") or {}
    if books:
        sb["got"].append(_line(f"строк: в отчётах {_n(t.get('rows_in_report'))}, принято {_n(t.get('rows_accepted'))}, "
                               f"в расчёте {_n(t.get('rows_in_run'))}"))
    for x in (P.get("losses") or [])[:5]:
        sb["dropped"].append(_line(f"{x['what']}: {x['count']}", tone="warn"))
    flagged = [x for x in books if set(x.get("flags") or []) & {"not_in_run", "rows_differ"}]
    store_why = f"в расчёте не все строки позиций: книг {len(flagged)}" if flagged else None
    if not books:
        _status(sb, "waiting")
        sb["waits"] = "позиции хотя бы одной книги"

    ib = flow.block("intraday", "process", "Сделки → позиции дня", "сделки по облигациям меняют позиции начала дня",
                    (f"c20-trades.html?date={d.isoformat()}", "Сделки дня"))
    it = lineage.get("intraday_trades")
    trades_why = None
    if not getattr(settings, "intraday_trades", True):
        _status(ib, "off")
        ib["got"].append(_line("расчёт по сделкам дня выключен"))
    elif run is None:
        _status(ib, "waiting")
        ib["waits"] = "прогон даты"
    elif not isinstance(it, dict):
        ib["got"].append(_line("в прогоне сделок дня нет"))
    else:
        ib["got"].append(_line(f"в позиции вошло сделок: {_n(it.get('applied', 0))}, новых позиций "
                               f"{_n(it.get('new_positions', 0))}"))
        for k, n in sorted((it.get("skipped") or {}).items(), key=lambda kv: -kv[1]):
            tone = "warn" if k in trades.PROBLEMS else None
            ib["dropped"].append(_line(f"{trades.STATE_TEXT.get(k, k)}: {n}", tone=tone))
    if tr is not None and tr["problems"]:
        trades_why = f"не вошли в позиции дня сделок: {tr['problems']}"
    if tr is not None and tr["trades"]:
        pend = next((s["n"] for s in tr["states"] if s["state"] == "pending"), 0)
        if pend and ib["status"] == "ok":
            _status(ib, "waiting")
            ib["waits"] = f"пересчёт: {pend} сделок пришли после прогона"

    vb = flow.block("value", "process", "Оценка ядром", "цены, риск и кривые позиций по срезу хаба",
                    ("c03-blotter.html" + (f"?run_id={run_id}" if run_id else ""), "Позиции"), fixes["indexes"])
    facts = None
    if run is None:
        _status(vb, "waiting")
        vb["waits"] = "позиции и закрытие хаба за дату" if d <= today else "дата ещё не наступила"
    else:
        facts = _safe(flow, vb, lambda: _positions_facts(con, run["run_id"]), con)
        label = store.slice_label(d, run["cut_type"], run["cut_ts"], run["run_type"])
        vb["got"].append(_line(f"прогон #{run['run_id']} · {label}", run["completed_at"]))
        vb["at"] = run["completed_at"].isoformat() if run["completed_at"] else None
        if facts:
            vb["got"].append(_line(f"позиций {_n(facts['positions'])}, оценено {_n(facts['valued'])}"))
            for x in facts["not_valued"]:
                vb["dropped"].append(_line(f"{x['text']}: {x['n']}", tone="warn"))
    if "прогон не удался" in (summary.get("problems") or []):
        _status(vb, "error")
        vb["dropped"].insert(0, _line("последний прогон даты не удался", tone="bad"))
    if summary.get("current_run_id") and not summary.get("default_run_id") and run:
        vb["got"].append(_line("основного прогона (закрытия) ещё нет: показан прогон внутри дня"))
    value_why = f"не оценено позиций: {facts['not_valued_n']}" if facts and facts["not_valued_n"] else None
    off = (lineage.get("positions") or {}).get("off_report") or {}
    swaps = ((lineage.get("positions") or {}).get("unlinked_swap_terms") or {}).get("trades")
    terms_why = (f"условия {off['invalid']} сделок не разобраны" if off.get("invalid") else
                 f"условия свопов и РЕПО без строки позиций: {swaps}" if swaps else None)

    ob = flow.block("ocp", "process", "Расчёт ОВП", "ОВП из отчёта и из позиций, сверка", ("c16-ocp.html", "ОВП"),
                    fixes["ovp"])
    oc = lineage.get("ocp") or {}
    gaps: list[dict[str, Any]] = []
    ocp_said: set[str] = set()   # what the block says itself: not again as «на входе неполные данные»
    if run is None:
        _status(ob, "waiting")
        ob["waits"] = "прогон даты"
    else:
        blk = _safe(flow, ob, lambda: _ocp_block(con, run["run_id"]), con) or {}
        gaps = blk.get("gaps") or []
        status = oc.get("status") or blk.get("status")
        ob["got"].append(_line(blk.get("text") or {"computed": "ОВП посчитана", "no_report": "ОВП только из позиций",
                                                   "failed": "ОВП не посчитана", "not_computed": "прогон без ОВП"}
                               .get(status or "", "сведений об ОВП нет")))
        if status == "failed":
            _status(ob, "error")
        elif status in (None, "not_computed"):
            _status(ob, "off")
        for g in gaps[:6]:   # the missing report is said once: by the line of the block's result above
            if not (status == "no_report" and g.get("code") == "no_report"):
                ob["dropped"].append(_line(g.get("text") or g.get("code"), tone="warn"))
        ocp_said = {x["t"] for x in ob["got"] + ob["dropped"]} | {
            g.get("text") for g in gaps if status == "no_report" and g.get("code") == "no_report"}
        if oc.get("breaks"):
            ob["dropped"].append(_line(f"расхождений с отчётом ОВП: {oc['breaks']}", tone="warn"))
    codes = {g.get("code") for g in gaps}
    ocp_report_why = next((g.get("text") for g in gaps if g.get("code") in OCP_REPORT_GAPS), None)
    if run is not None and (oc.get("status") == "no_report") and not ocp_report_why:
        ocp_report_why = "ОВП посчитана без отчёта ОВП"
    cash_why = None
    if run is not None:
        if codes & set(CASH_GAPS):
            cash_why = next(g.get("text") for g in gaps if g.get("code") in CASH_GAPS)
        elif not cash_units and declared.get("cash_balances"):
            cash_why = "ОВП посчитана без остатков на счетах"

    pb = flow.block("pnl", "process", "Расчёт PnL", "PnL позиций к прошлому закрытию, PnL новых сделок и допущения",
                    ("c10-pnl-explain.html", "PnL explain"))
    pnl_why = None
    if run is None:
        _status(pb, "waiting")
        pb["waits"] = "прогон даты"
    elif facts:
        pb["got"].append(_line(f"PnL посчитан у позиций: {_n(facts['with_pnl'])} из {_n(facts['positions'])}"))
        if facts["assumed"]:
            pb["got"].append(_line(f"с допущениями: {_n(facts['assumed'])}", tone="warn"))
        for x in facts["no_pnl"]:
            pb["dropped"].append(_line(f"{x['text']}: {x['n']}", tone="warn"))
        if not facts["with_pnl"] and facts["positions"]:
            _status(pb, "waiting")
            pb["waits"] = "закрытие предыдущего рабочего дня"
        missing = facts["positions"] - facts["with_pnl"]
        if missing > 0:
            pnl_why = f"PnL без {missing} позиций"
        if it and isinstance(it, dict) and it.get("applied"):
            pb["got"].append(_line(f"PnL новых сделок: сделок {_n(it['applied'])}"))

    kb = flow.block("backfill", "process", "Пересчёт прошлых дат", "история прогонов: пересчёт дат после изменений",
                    ("c11-history.html", "Динамика"), fixes["bf"])
    hist_runs_why = None
    if bf is None:
        _status(kb, "error")
        kb["dropped"].append(_line("состояние истории прогонов не прочитано", tone="bad"))
    else:
        total = bf.get("total") or 0
        kb["progress"] = {"done": bf.get("done") or 0, "total": total, "text": "дат посчитано"}
        kb["got"].append(_line(f"дат с {_dm(bf.get('from'))}: посчитано {bf.get('done') or 0} из {total}"))
        for x in (bf.get("todo_by_reason") or [])[:4]:
            kb["got"].append(_line(f"в очереди — {x['text']}: {x['n']}"))
        for x in (bf.get("problem_dates") or [])[:4]:
            kb["dropped"].append(_line(f"{_dm(x['date'])}: {x.get('why') or x.get('kind')}", tone="bad"))
        if bf.get("no_positions"):
            kb["dropped"].append(_line(f"дат без позиций: {bf['no_positions']}"))
        if bf.get("no_market"):
            kb["dropped"].append(_line(f"дат без закрытия хаба: {bf['no_market']}"))
        if not bf.get("enabled"):
            _status(kb, "off")
            kb["got"].insert(0, _line("пересчёт истории выключен"))
        elif bf.get("stalled"):
            _status(kb, "waiting")
            kb["waits"] = "повтор после ошибки хранилища или сети"
        elif bf.get("todo"):
            _status(kb, "waiting")
            kb["waits"] = f"очередь: {bf['todo']} дат"
        if bf.get("failed"):
            _status(kb, "error")
        if bf.get("todo") and bf.get("enabled"):
            hist_runs_why = f"история не полная: {bf['todo']} дат в очереди"
            flow.ahead.append({"id": "backfill", "title": "Пересчёт прошлых дат",
                               "text": f"осталось дат: {bf['todo']}" + (
                                   f", следующие: {', '.join(_dm(x) for x in (bf.get('next') or [])[:3])}"
                                   if bf.get("next") else ""),
                               "at": None, "eta_s": bf.get("eta_s"), "progress": kb["progress"]})

    lb = flow.block("history_load", "process", "Догрузка истории", "история фиксингов и рынка из хаба по частям",
                    ("c09-market.html", "Рынок"), fixes["hub"])
    jobs = fx.get("jobs") or {}
    hub_why = None
    if bf is not None:
        done, pend, errs = jobs.get("done") or 0, jobs.get("pending") or 0, jobs.get("errors") or 0
        if done + pend + errs:
            lb["progress"] = {"done": done, "total": done + pend + errs, "text": "запросов к хабу выполнено"}
            lb["got"].append(_line(f"запросов: выполнено {done}, в очереди {pend}", jobs.get("fetched_at")))
        if errs:
            lb["dropped"].append(_line(f"запросов с ошибкой: {errs}", tone="bad"))
            hub_why = f"часть истории фиксингов не загружена: запросов с ошибкой {errs}"
        if not fx.get("loading"):
            _status(lb, "off")
            lb["got"].append(_line("догрузка истории выключена"))
        elif pend:
            _status(lb, "waiting")
            lb["waits"] = f"очередь: {pend} запросов"
            flow.ahead.append({"id": "history_load", "title": "Догрузка истории из хаба",
                               "text": f"запросов в очереди: {pend}", "at": None, "eta_s": None,
                               "progress": lb["progress"]})

    # the autopilot's step now
    if running_block in flow.blocks:
        rbk = flow.blocks[running_block]
        rbk["status"] = "running"
        rbk["progress"] = {"done": prog.get("done") or 0, "total": prog.get("total"),
                           "text": prog.get("detail") or ""} if prog else rbk["progress"]
        flow.ahead.insert(0, {"id": "now", "title": "Сейчас", "text": ap.get("message") or rbk["title"],
                              "at": None, "eta_s": None, "progress": rbk["progress"]})
    if ap.get("state") == "error" and ap.get("message"):
        flow.ahead.insert(0, {"id": "autopilot", "title": "Автопилот: ошибка", "text": ap["message"], "at": None,
                              "eta_s": None, "tone": "bad"})

    # ---------------------------------------------------------------- results
    rp = flow.block("r_positions", "result", "Позиции и риск", "C03 «Позиции», C02 «Лестница рисков», C01 «Кокпит»",
                    ("c03-blotter.html" + (f"?run_id={run_id}" if run_id else ""), "Позиции"))
    rt = flow.block("r_trades", "result", "Сделки дня", "C20 «Сделки дня», плитка на кокпите",
                    (f"c20-trades.html?date={d.isoformat()}", "Сделки дня"))
    ro = flow.block("r_ocp", "result", "ОВП", "C16 «ОВП», ОВП в шапке", ("c16-ocp.html", "ОВП"))
    rn = flow.block("r_pnl", "result", "PnL дня", "C05 «Утренний брифинг», C10 «PnL explain»",
                    ("c05-morning-brief.html", "Брифинг"))
    rh = flow.block("r_history", "result", "История и PnL за период", "C11 «Динамика», C19 «PnL за период»",
                    ("c19-pnl-history.html", "PnL за период"))
    for x in (rp, ro, rn):
        if run is None:
            _status(x, "waiting")
            x["waits"] = "прогон даты"
    if facts:
        rp["got"].append(_line(f"позиций {_n(facts['positions'])}, книг {len(books)}"))
        rn["got"].append(_line(f"PnL у позиций: {_n(facts['with_pnl'])} из {_n(facts['positions'])}"))
    if tr is not None:
        # the time of the last trade as C20 shows it: HH:MM of trade_ts as is, no time zone (not ``at``: the page
        # puts an ``at`` on the Moscow clock)
        last = f", последняя в {str(tr['last_trade_ts'])[11:16]}" if tr["last_trade_ts"] else ""
        rt["got"].append(_line(f"сделок {_n(tr['trades'])}, в позициях {_n(tr['applied'])}{last}"))
        if not tr["trades"]:
            not_come(rt, "trades", f"сделки за {dd}")
    if run is not None:
        ro["got"].append(_line(ob["got"][0]["t"] if ob["got"] else "ОВП"))
        if ob["status"] in ("error", "off"):
            _status(ro, ob["status"])
    if bf is not None:
        rh["got"].append(_line(f"основных прогонов с {_dm(bf.get('oldest_run'))}"))
        if bf.get("todo") and bf.get("enabled"):
            rh["got"].append(_line(f"ещё не пересчитано дат: {bf['todo']}", tone="warn"))

    # ---------------------------------------------------------------- edges
    def ran(why: str | None) -> str | None:   # into the valuation and on: only a run took the incomplete data on
        return why if run is not None else None

    flow.edge("positions", "read", "отчёты о позициях", ran(pos_why))
    flow.edge("ocp_report", "ocp", "отчёт ОВП", ocp_report_why)
    flow.edge("cash", "ocp", "остатки на счетах", cash_why)
    flow.edge("pnl_official", "pnl", "официальный PnL для сверки")
    flow.edge("trades", "intraday", "сделки дня", trades_why)
    flow.edge("terms", "value", "условия сделок", ran(terms_why))
    flow.edge("past_trades", "backfill", "сделки прошлых дат", hist_why)
    flow.edge("market", "value", "срез рынка", ran(market_why))
    flow.edge("hub_history", "history_load", "фиксинги и история рынка", None)
    flow.edge("reference", "store", "справочник книг")
    flow.edge("reference", "ocp", "политика ОВП", ref_why)
    flow.edge("read", "store", "строки отчётов", read_why)
    flow.edge("store", "intraday", "позиции начала дня")
    flow.edge("store", "value", "позиции", ran(store_why or pos_why))
    flow.edge("intraday", "value", "позиции с учётом сделок", ran(trades_why))
    flow.edge("history_load", "value", "фиксинги для окна среза", ran(hub_why))
    flow.edge("history_load", "backfill", "история для прошлых дат", hub_why)
    flow.edge("value", "ocp", "валютная дельта, PV деривативов", value_why)
    flow.edge("value", "pnl", "оценка сегодня и вчера", value_why)
    flow.edge("value", "r_positions", "позиции, риск", value_why)
    flow.edge("intraday", "r_trades", "сделки и их учёт", ran(trades_why))
    flow.edge("ocp", "r_ocp", "ОВП", ran(cash_why or ocp_report_why or (f"ОВП с оговорками: {len(gaps)}" if gaps else None)))
    flow.edge("pnl", "r_pnl", "PnL", ran(pnl_why))
    flow.edge("backfill", "r_history", "прогоны прошлых дат", hist_runs_why)
    for x in flow.blocks.values():   # a result takes its input's waiting or error (RESULT_TAKES)
        if x["column"] != "result":
            continue
        for e in flow.edges:
            src = flow.blocks.get(e["from"])
            if e["to"] == x["id"] and src and src["status"] in RESULT_TAKES:
                worse = ORDER.index(src["status"]) > ORDER.index(x["status"]) or x["status"] == "ok"
                _status(x, src["status"])
                if worse and x["status"] == "waiting":
                    x["waits"] = f"«{src['title']}»" + (f": {src['waits']}" if src.get("waits") else "")
                elif worse:
                    x["dropped"].insert(0, _line(f"ошибка на шаге «{src['title']}»", tone="bad"))
    for x in flow.blocks.values():   # a block that was given incomplete data says so (once: not what it said itself)
        for e in flow.edges:
            if e["to"] == x["id"] and e["incomplete"] and x["status"] == "ok" \
                    and e["why"] not in (ocp_said if x["id"] == "ocp" else ()):
                x["dropped"].append(_line(f"на входе неполные данные: {e['why']}", tone="warn"))

    # reports still awaited
    for x in flow.blocks.values():
        if x["column"] == "source" and x["status"] == "waiting" and x["waits"]:
            flow.ahead.append({"id": f"wait:{x['id']}", "title": f"Ждём: {x['title']}", "text": x["waits"],
                               "at": None, "eta_s": None})
    for x in flow.blocks.values():
        x["status_text"] = STATUS[x["status"]]
    counts = Counter(x["status"] for x in flow.blocks.values())
    return {"date": d.isoformat(), "generated_at": now.isoformat(), "statuses": STATUS, "columns": COLUMNS,
            "run": ({"run_id": run["run_id"], "label": store.slice_label(d, run["cut_type"], run["cut_ts"],
                                                                         run["run_type"]),
                     "is_default": run["run_id"] == summary.get("default_run_id")} if run else None),
            "day_state": summary.get("state"), "blocks": list(flow.blocks.values()), "edges": flow.edges,
            "ahead": flow.ahead, "counts": dict(counts),
            "incomplete": sum(1 for e in flow.edges if e["incomplete"]),
            "autopilot": {"state": ap.get("state") or "off", "message": ap.get("message"),
                          "enabled": bool(ap.get("enabled"))}}


REFERENCE_NAMES = {"entities": "юрлица", "funding_rules": "правила фондирования", "ocp_policy": "политика ОВП",
                   "funding_policy": "политика фондирования"}


def _reference(con: psycopg.Connection, d: date) -> dict[str, Any]:
    books = sum(1 for b in store.current_books(con, d) if b.get("active", True))
    kinds = {k: (n, at) for k, n, at in con.execute(
        "SELECT kind, count(DISTINCT ref_key), max(recorded_at) FROM reference_rev WHERE active GROUP BY 1 ORDER BY 1")}
    return {"books": books, "kinds": kinds}


_BF_LOCK = threading.Lock()
_BF_CACHE: dict[tuple[str, str, date], tuple[float, dict[str, Any]]] = {}
_clock = time.monotonic


def _backfill(con: psycopg.Connection, settings: Settings) -> dict[str, Any]:
    """``backfill.status`` of this store and settings, kept ``BACKFILL_TTL_S`` seconds: the screen asks every minute
    per tab, and the status reads every date of the history (a year: thousands of queries)."""
    from . import backfill

    today = datetime.now(UTC).astimezone(store.MSK).date()
    # the settings' whole text in the key: a change of the history settings is shown at once, not after the window
    key = (str(con.info.dbname) + "@" + str(con.info.host) + ":" + str(con.info.port), str(settings.home), today,
           hashlib.sha256(repr(settings).encode()).hexdigest())
    with _BF_LOCK:
        hit = _BF_CACHE.get(key)
        if hit is not None and _clock() - hit[0] < BACKFILL_TTL_S:
            return hit[1]
    v = backfill.status(con, settings, today)
    with _BF_LOCK:
        for k in [k for k, (t, _) in _BF_CACHE.items() if _clock() - t >= BACKFILL_TTL_S]:
            del _BF_CACHE[k]
        _BF_CACHE[key] = (_clock(), v)
    return v


def forget() -> None:
    """Drop the kept history of runs (tests; a change the screen must show at once)."""
    with _BF_LOCK:
        _BF_CACHE.clear()


def _as_date(v: Any) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _ovp_fix(settings: Settings) -> tuple[str, str] | None:
    """The settings' section of the OCP («ОВП: типы строк») only when it has something: it opens with the module of
    the adapter's choice ``ocp_derivative_types`` (TDM#554); else no link (the section stays hidden)."""
    from .inbox import scan

    try:
        h = scan.adapter(settings)
        if h.ok and any(a.get("choices") == "ocp_derivative_types" for a in h.actions or ()):
            return ("../settings.html#ovp", "ОВП: типы строк")
    except Exception:  # noqa: BLE001 - no adapter: no link
        return None
    return None
