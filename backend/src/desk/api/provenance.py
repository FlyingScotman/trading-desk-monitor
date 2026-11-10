"""Where a position's data came from (TDM#617): the position card's collapsed section «Откуда данные».

``provenance(con, run, pid, feeds)`` is ``GET /api/v1/positions/provenance``: for one position of a run

- ``sources``: each source and whether it has data on this deal, with the dataset (``daystatus.DATASET_NAMES``), the
  adapter's source id, the file name, the report date and when the monitor stored it:

  - ``positions``: the row of the positions report among the run's own sets (``run.position_set_ids``);
  - ``trade_terms``: the deal's terms (``valuation.derivatives.link``: the legs naming the row's ``position_id``, else
    the trade of its ``deal_id``, else of its ``position_id``; a valued derivative: the trades core valued,
    ``extra.core.trades``; a derivative off the report: its own trade), from the sets the run read
    (``lineage.positions.trade_terms``; a book without them: the sets in force at ``positions_known_at``);
  - ``trades``: the day's trades of the trades service the run read (``lineage.intraday_trades.set_ids``; a run of a
    version that records it without the block: none came by then, or the setting was off) that name the
    position, its deal or a trade the run put in it, in its book, with where the file came from
    (``trades.origin_of``), the feed's title when only one feed delivers trades, and the trade's state in the day's book
    as the trades screen says it (``trades._intraday_state``; a trade of an earlier date: the start of the day);
  - ``ocp`` and ``cash_balances``: by book and currency only (no key of a deal), the sets in force at the run's
    ``positions_known_at`` (``store.dataset_set_ids``), and whether a newer report came after the run;

- ``mismatches``: the sources in words (``coverage.MISMATCH_TEXT``): a report row without terms, a position the trades
  opened that the report does not have, terms without a report row, one deal on several report rows, a report that
  differs from the start of the day plus the trades;
- ``market``: the market data the valuation used (a valued position: its fact; a derivative counts as valued only when
  core valued it from its terms, not with the report's value it got instead) or, for an unvalued derivative, what its
  terms need (RUB curve; per foreign currency its FX spot and the curve the valuation would take, by the run's setting
  ``fx_foreign_curve`` and a CCS leg's XCCY first, a fallback only: ``replaced``; a floating index its curve and
  fixings; an NDF the CBR fixing of each foreign currency of its pair, both of a cross NDF), each with its
  state in the run's market (``coverage.MARKET_STATE``): every need, not just the first failure. Its notes say
  why a derivative valued without core has no rates risk (``extra.risk_missing``, TDM#616) and, for an NDF
  whose terms name no settlement currency, the one taken.

What an old run did not record is ``unknown`` («нет сведений»), never an error; missing data is ``absent`` / ``missing``,
never a zero. Nothing here reads a report's values beyond keys and dates."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from .. import store, trades
from ..curves import fx as fxcurves
from ..daystatus import DATASET_NAMES
from ..valuation import derivatives as dv
from ..valuation.positions import RISK_MISSING
from . import concepts
from . import coverage as cv

TERMS_KINDS = ("otc",)
"""Report rows that are derivatives (valued from their terms); an FX row too when it settles after the date (a
forward), and a row without ``asset_class`` but with a ``deal_id`` (TDM#501) — as ``valuation.positions``."""
BOOK_CCY = ("ocp", "cash_balances")
CURVE_NAMES = {"RUB_RUONIA_OIS": "RUB RUONIA", "RUB_KEY_RATE_PROJ": "RUB KEY", "RUB_KEY_RATE_SWAP": "RUB KEY"}
"""Core's curve id -> the published curve (``valuation.bonds.RISK_CURVES``)."""
_CCY = re.compile(r"^[A-Z]{3}$")


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, date | datetime) else v


def _ccy(v: Any) -> str | None:
    s = str(v or "").strip().upper()
    s = "CNY" if s == "CNH" else "RUB" if s in ("RUR", "SUR") else s
    return s if _CCY.match(s) else None


def _set_item(dataset: str, s: dict[str, Any], **more: Any) -> dict[str, Any]:
    """The common part of an item: the dataset's name, the adapter's source id, the file, the report date and when the
    monitor stored the set."""
    return {"dataset": dataset, "dataset_name": DATASET_NAMES.get(dataset, dataset), "source": s.get("source"),
            "file_name": s.get("source_name"), "report_date": _iso(s.get("report_date")),
            "valid_date": _iso(s.get("valid_date")), "loaded_at": _iso(s.get("recorded_at")), **more}


def _source(key: str, level: str, state: str, text: str, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"key": key, "name": DATASET_NAMES.get(key, key), "level": level, "level_text": cv.PROVENANCE_LEVEL[level],
            "state": state, "state_text": cv.PROVENANCE_STATE[state], "text": text, "items": items or []}


# ---- the positions report ---------------------------------------------------------------------------------------

def _report(con, run: dict[str, Any], pid: str, book: str) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    """(the source, the report row's fields or None, how many rows of the book share its ``deal_id``)."""
    ids = list(run.get("position_set_ids") or [])
    sets = {s["set_id"]: s for s in con.execute(
        "SELECT set_id, source, source_name, report_date, report_ts, recorded_at, valid_date FROM position_set"
        " WHERE set_id = ANY(%s) AND book_id = %s", (ids, book)).fetchall()}
    if not sets:
        return _source("positions", "deal", "absent", cv.PROVENANCE_TEXT["report_no_book"]), None, 0
    row = con.execute(
        "SELECT set_id, position_id, deal_id, asset_class, ccy, value_date, src, x FROM position_input"
        " WHERE set_id = ANY(%s) AND position_id = %s ORDER BY set_id LIMIT 1", (list(sets), pid)).fetchone()
    if row is None:
        items = [_set_item("positions", s) for s in sets.values()]
        return _source("positions", "deal", "absent", cv.PROVENANCE_TEXT["report_no_row"], items), None, 0
    same = 1
    if row.get("deal_id"):
        same = con.execute("SELECT count(*) AS n FROM position_input WHERE set_id = ANY(%s) AND deal_id = %s",
                           (list(sets), row["deal_id"])).fetchone()["n"]
    item = _set_item("positions", sets[row["set_id"]], ref=row.get("src"), deal_id=row.get("deal_id"))
    return _source("positions", "deal", "present", cv.PROVENANCE_TEXT["report_row"], [item]), dict(row), int(same)


# ---- the deal's terms -------------------------------------------------------------------------------------------

def _terms_sets(con, run: dict[str, Any], book: str) -> list[int] | None:
    """The trade terms sets the run read for ``book``: its lineage's units, else (a book without a positions set, an
    off-report trade's) the sets in force on the date at ``positions_known_at``; None: the run's lineage has no
    positions block (no information)."""
    pos = (run.get("lineage") or {}).get("positions")
    if not isinstance(pos, dict):
        return None
    units = [u for u in pos.get("trade_terms") or () if isinstance(u, dict)
             and str((u.get("scope") or {}).get("book_id")) == book and u.get("set_id") is not None]
    if units:
        return sorted({int(u["set_id"]) for u in units})
    return [r["set_id"] for r in con.execute(
        "SELECT DISTINCT ON (scope_key) set_id FROM dataset_set WHERE dataset = 'trade_terms' AND valid_date <= %s"
        f" AND recorded_at <= %s ORDER BY scope_key, {store.CURRENT_SET}",
        (run["business_date"], run["positions_known_at"])).fetchall()]


def _terms(con, run: dict[str, Any], pid: str, book: str, x: dict[str, Any], fact: dict[str, Any] | None,
           off_trade: str | None) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], bool]:
    """(the deal's terms rows with ``_set_id``, their sets by id, whether the run's lineage says what it read)."""
    ids = _terms_sets(con, run, book)
    if ids is None:
        return [], {}, False
    core = ((fact or {}).get("extra") or {}).get("core") or {}
    wanted = {str(t) for t in core.get("trades") or ()}
    if off_trade:
        wanted.add(off_trade)
    # the candidate trades of the link rule (``derivatives._link``): the legs naming the position, the trade of its
    # deal_id or of its position_id; then every leg of those trades of the book
    names = sorted(wanted | {str(v) for v in (x.get("deal_id"), pid) if v})
    rows = [{**r["data"], "_set_id": r["set_id"]} for r in con.execute(
        "SELECT set_id, data FROM dataset_row WHERE set_id = ANY(%(ids)s) AND data->>'book_id' = %(b)s"
        " AND data->>'trade_id' IN (SELECT data->>'trade_id' FROM dataset_row WHERE set_id = ANY(%(ids)s)"
        "  AND data->>'book_id' = %(b)s AND (data->>'position_id' = %(p)s OR data->>'trade_id' = ANY(%(n)s)))"
        " ORDER BY set_id, row_no", {"ids": ids, "b": book, "p": pid, "n": names}).fetchall()]
    if wanted:   # as ``derivatives._link``: a leg naming another position is that position's
        got = [r for r in rows if str(r.get("trade_id")) in wanted
               and (not r.get("position_id") or str(r["position_id"]) == pid)]
    else:
        linked, _, _ = dv.link([(pid, book, x)], rows)
        got = linked.get(pid, [])
    sets = {s["set_id"]: s for s in con.execute(
        "SELECT set_id, source, source_name, report_date, recorded_at, valid_date FROM dataset_set WHERE set_id = ANY(%s)",
        (sorted({r["_set_id"] for r in got}),)).fetchall()} if got else {}
    return got, sets, True


def _terms_source(kind: str, got: list[dict[str, Any]], sets: dict[int, dict[str, Any]], known: bool,
                  any_units: bool, by_ticker: bool = False) -> dict[str, Any]:
    if got:
        items = [_set_item("trade_terms", sets.get(r["_set_id"], {}), trade_id=r.get("trade_id"),
                           leg_id=r.get("leg_id"), product_kind=r.get("product_kind")) for r in got]
        return _source("trade_terms", "deal", "present", cv.PROVENANCE_TEXT["terms_rows"], items)
    if kind == "bond":
        return _source("trade_terms", "deal", "not_needed", cv.PROVENANCE_TEXT["terms_bond"])
    if by_ticker:
        return _source("trade_terms", "deal", "not_needed", cv.PROVENANCE_TEXT["terms_by_ticker"])
    if kind == "other":
        return _source("trade_terms", "deal", "not_needed", cv.PROVENANCE_TEXT["terms_not_needed"])
    if not known:
        return _source("trade_terms", "deal", "unknown", cv.PROVENANCE_TEXT["no_lineage"])
    return _source("trade_terms", "deal", "absent",
                   cv.PROVENANCE_TEXT["terms_none" if any_units else "terms_book_none"])


# ---- the day's trades -------------------------------------------------------------------------------------------

TRADES_LINEAGE_FROM = (0, 2, 18)
"""The first version whose runs say in their lineage which trades of the day they read (``intraday_trades``, TDM#557)."""


def _version(v: Any) -> tuple[int, ...] | None:
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def _trade_sets(con, run: dict[str, Any]) -> tuple[list[int] | None, dict[str, Any] | None, str]:
    """(the trades sets the run read, its ``intraday_trades`` lineage, why there are none): the lineage's set ids; a run
    of a version that records them but has no block read none — no trades sets of the date yet at its
    ``positions_known_at`` (``no_trades``), else the setting ``intraday_trades`` was off (``off``: the sets of the date
    then); an older run: None (``no_lineage``)."""
    t = (run.get("lineage") or {}).get("intraday_trades")
    if isinstance(t, dict) and "set_ids" in t:
        return list(t.get("set_ids") or []), t, ""
    v = _version(run.get("backend_version"))
    if v is None or v < TRADES_LINEAGE_FROM:
        return None, None, "no_lineage"
    ids = [r["set_id"] for r in con.execute(
        "SELECT set_id FROM dataset_set WHERE dataset = 'trades' AND valid_date = %s AND recorded_at <= %s"
        " ORDER BY set_id", (run["business_date"], run.get("positions_known_at") or run.get("started_at"))).fetchall()]
    return ids, None, "off" if ids else "no_trades"


def _trades(con, run: dict[str, Any], pid: str, book: str, ids: set[str], feeds: list[dict[str, Any]] | None,
            history: dict[str, set[str]] | None = None) -> tuple[dict[str, Any], int]:
    """(the source, how many trades name the position): the current version of each trade of the sets the run read
    (``_trade_sets``) that names the position (its ``position_id``) or one of ``ids`` (the deal, its terms' trades, the
    trades the run put in the position) in any of its versions, as ``store.versioned_rows`` takes it (the highest
    version, none below any; an equal one: the newer set by ``CURRENT_SET``), and only when that version is of the
    position's book (an amended trade can move)."""
    set_ids, t, why = _trade_sets(con, run)
    if set_ids is None:
        return _source("trades", "deal", "unknown", cv.PROVENANCE_TEXT["trades_no_lineage"]), 0
    if not set_ids:
        return _source("trades", "deal", "absent", cv.PROVENANCE_TEXT["trades_no_sets"]), 0
    rows = con.execute(
        "WITH s AS (SELECT set_id, source, source_name, report_date, recorded_at, valid_date,"
        f" row_number() OVER (PARTITION BY valid_date ORDER BY {store.CURRENT_SET}) AS age"
        " FROM dataset_set WHERE set_id = ANY(%(ids)s))"
        " SELECT r.set_id, r.data, s.source, s.source_name, s.report_date, s.recorded_at, s.valid_date, s.age"
        " FROM dataset_row r JOIN s USING (set_id) WHERE r.data->>'trade_id' IN (SELECT data->>'trade_id'"
        "  FROM dataset_row WHERE set_id = ANY(%(ids)s) AND (data->>'position_id' = %(p)s OR data->>'trade_id' = ANY(%(n)s)))"
        " ORDER BY r.set_id", {"ids": set_ids, "p": pid, "n": sorted(ids)}).fetchall()
    cur: dict[tuple, tuple[tuple, dict[str, Any]]] = {}
    for r in rows:   # per (date, trade) as ``store.versioned_rows``
        k = (r["valid_date"], str(r["data"].get("trade_id")))
        v = r["data"].get("trade_version")
        try:
            rank = (-1 if v is None else int(v), -r["age"])
        except (TypeError, ValueError):
            rank = (-1, -r["age"])
        if k not in cur or rank > cur[k][0]:
            cur[k] = (rank, r)
    # a trade whose current version is of another book, or names another position (and is none of ``ids``), is not this
    # position's
    found = {k: r for k, (_, r) in cur.items() if str(r["data"].get("book_id")) == book
             and (k[1] in ids or not r["data"].get("position_id") or str(r["data"]["position_id"]) == pid)}
    if not found:
        return _source("trades", "deal", "absent", cv.PROVENANCE_TEXT["trades_none"]), 0
    origins = {r["set_id"]: trades.origin_of(r["path"], r["at"], r["valid_date"], r["sha256"], history) for r in con.execute(
        "SELECT s.set_id, s.recorded_at AS at, s.valid_date, f.path, f.sha256 FROM dataset_set s"
        " JOIN inbox_file f ON f.sha256 = s.inbox_file_sha256 WHERE s.set_id = ANY(%s)",
        (sorted({r["set_id"] for r in found.values()}),)).fetchall()}
    # the feed's title only when one feed delivers trades: the folder of a feed's run does not say which feed it was
    titles = [f["title"] for f in feeds or () if f.get("dataset") == "trades" and f.get("title")]
    title = titles[0] if len(titles) == 1 else None
    run_t = {"versions": (t or {}).get("versions") or {}, "skipped_ids": (t or {}).get("skipped_ids") or {},
             "truncated": bool((t or {}).get("truncated"))}
    bd = run["business_date"]
    items = []
    for (vd, tid), r in sorted(found.items(), key=lambda kv: (kv[0][0] or bd, kv[0][1])):
        d = r["data"]
        if vd is not None and vd < bd:   # a trade of an earlier date: in the start of the day of a carried book
            state, text = "earlier_date", cv.PROVENANCE_TEXT["trade_earlier"].format(day=vd.isoformat())
        else:   # as the trades screen says it (``trades._intraday_state``: applied only at the version the run took)
            state = trades._intraday_state(d, bd, run_t, why != "off")
            text = trades.STATE_TEXT.get(state, state)
        origin = origins.get(r["set_id"])
        items.append(_set_item("trades", r, trade_id=tid, trade_version=d.get("trade_version"), status=d.get("status"),
                               side=d.get("side"), state=state, state_text=text,
                               origin=origin, origin_text=trades.ORIGIN_TEXT.get(origin or ""),
                               feed_title=title if origin in ("feed", "history") else None))
    text = cv.PROVENANCE_TEXT["trades_rows"] + ("; " + cv.PROVENANCE_TEXT["trades_off"] if why == "off" else "")
    return _source("trades", "deal", "present", text, items), len(items)


# ---- the OCP report and the balances: by book and currency ------------------------------------------------------

def _set_ids(con, bd: date, known_at: datetime) -> dict[str, list[int]]:
    """``store.dataset_set_ids`` of ``BOOK_CCY`` on the API's connection (dict rows)."""
    return {r["dataset"]: list(r["ids"]) for r in con.execute(
        "SELECT dataset, array_agg(set_id ORDER BY set_id) AS ids FROM (SELECT DISTINCT ON (dataset, scope_key) dataset,"
        " set_id FROM dataset_set WHERE dataset = ANY(%s) AND valid_date <= %s AND recorded_at <= %s"
        f" ORDER BY dataset, scope_key, {store.CURRENT_SET}) s GROUP BY dataset", (list(BOOK_CCY), bd, known_at))}


def _book_sets(con, ids: list[int], book: str, entity: str | None) -> list[int]:
    """Of ``ids`` the sets of ``book``: its scope is the book, or (a scope without a book) it has rows of the book or is
    of the book's legal entity."""
    return [r["set_id"] for r in con.execute(
        "SELECT s.set_id FROM dataset_set s WHERE s.set_id = ANY(%(ids)s) AND (s.scope->>'book_id' = %(b)s"
        " OR (NOT s.scope ? 'book_id' AND (s.scope->>'entity' = %(e)s OR EXISTS (SELECT 1 FROM dataset_row r"
        "  WHERE r.set_id = s.set_id AND r.data->>'book_id' = %(b)s)))) ORDER BY s.set_id",
        {"ids": ids, "b": book, "e": entity}).fetchall()] if ids else []


def _book_ccy(con, run: dict[str, Any], book: str, ccys: list[str]) -> list[dict[str, Any]]:
    """Per dataset of ``BOOK_CCY`` the sets in force at the run's ``positions_known_at`` and, per currency of the
    position (the OCP: foreign only; a RUB-only deal: not needed), whether a row has it, at the level the report has
    (``rules.ocp``): a row of the book (``by_book``), else, an OCP row without ``book_id`` (optional in the OCP; the
    cash balances always name the book) in a set of the book's scope (``by_book``) or of the book's legal entity
    (``by_entity``); ``later``: the sets of the book in force now differ."""
    bd, then = run["business_date"], run.get("positions_known_at")
    if then is None:
        return [_source(ds, "book_ccy", "unknown", cv.PROVENANCE_TEXT["no_lineage"]) for ds in BOOK_CCY]
    ref = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'books'", (run["run_id"],)).fetchone()
    entity = next((b.get("entity") for b in ((ref or {}).get("data") or []) if isinstance(b, dict)
                   and b.get("book_id") == book), None)
    at = _set_ids(con, bd, then)
    now = _set_ids(con, bd, con.execute("SELECT now() AS t").fetchone()["t"])
    out = []
    for ds in BOOK_CCY:
        ids = at.get(ds, [])
        later = _book_sets(con, now.get(ds, []), book, entity) != _book_sets(con, ids, book, entity)
        want = [c for c in ccys if c != "RUB"] if ds == "ocp" else ccys
        if ds == "ocp" and not want:   # no foreign currency in the deal: no OCP to have
            out.append(_source(ds, "book_ccy", "not_needed", cv.PROVENANCE_TEXT["ocp_rub"]))
            continue
        rows = con.execute(
            "SELECT r.set_id, r.data->>'ccy' AS ccy, nullif(r.data->>'book_id', '') AS row_book,"
            " s.scope->>'book_id' AS scope_book, s.source, s.source_name, s.report_date, s.recorded_at, s.valid_date"
            " FROM dataset_row r JOIN dataset_set s USING (set_id) WHERE r.set_id = ANY(%(ids)s)"
            " AND (r.data->>'book_id' = %(b)s OR (nullif(r.data->>'book_id', '') IS NULL"
            "  AND (s.scope->>'book_id' = %(b)s OR (NOT s.scope ? 'book_id' AND s.scope->>'entity' = %(e)s"
            # an entity's rows do not stand for a book the same set gives rows of its own (``rules.ocp``: ``at_of``)
            "   AND NOT EXISTS (SELECT 1 FROM dataset_row r2 WHERE r2.set_id = s.set_id AND r2.data->>'book_id' = %(b)s)))))",
            {"ids": ids, "b": book, "e": entity}).fetchall() if ids else []
        by_ccy: dict[str, dict[str, Any]] = {}
        for r in rows:   # a row of the book before one of its entity
            c = _ccy(r["ccy"])
            lvl = "by_book" if r["row_book"] == book or r["scope_book"] == book else "by_entity"
            if c and (c not in by_ccy or (lvl == "by_book" and by_ccy[c]["_lvl"] == "by_entity")):
                by_ccy[c] = {**r, "_lvl": lvl}
        items = []
        for c in want:
            r = by_ccy.get(c)
            items.append(_set_item(ds, r, ccy=c, has_row=True, level=r["_lvl"], level_text=cv.PROVENANCE_ROW_LEVEL[r["_lvl"]])
                         if r else
                         {"dataset": ds, "dataset_name": DATASET_NAMES.get(ds, ds), "ccy": c, "has_row": False,
                          "source": None, "file_name": None, "report_date": None, "valid_date": None, "loaded_at": None})
        has = any(i["has_row"] for i in items)
        entity_only = has and all(i.get("level") == "by_entity" for i in items if i["has_row"])
        text = cv.PROVENANCE_TEXT["book_ccy_rows" if has else "book_ccy_none"]
        if entity_only:
            text = cv.PROVENANCE_TEXT["entity_ccy_rows"]
        if later:
            text += "; " + cv.PROVENANCE_TEXT["book_ccy_later"]
        src = _source(ds, "book_ccy", "present" if has else "absent", text, items)
        if entity_only:
            src.update(level="entity_ccy", level_text=cv.PROVENANCE_LEVEL["entity_ccy"])
        out.append(src)
    return out


# ---- the market data --------------------------------------------------------------------------------------------

def _need(kind: str, cid: str, name: str, state: str, text: str = "") -> dict[str, Any]:
    return {"kind": kind, "id": cid, "name": name, "state": state, "state_text": cv.MARKET_STATE[state], "text": text}


def _curve_state(lin: dict[str, Any], cid: str) -> tuple[str, str]:
    """The state of a foreign curve in the run's market (``lineage.fx.curves``)."""
    info = ((lin.get("fx") or {}).get("curves") or {}).get(cid)
    if not isinstance(info, dict):
        return "missing", "в рыночных данных прогона кривой нет"
    if info.get("status") != "ok":
        return "missing", "кривая не построена" + (f": {info.get('status')}" if info.get("status") else "")
    if info.get("stale"):
        return "stale", "котировки кривой не на дату прогона"
    if info.get("proxy"):
        return "replaced", "вместо OIS — прокси по гособлигациям" + (
            f" ({info['source_curve']})" if info.get("source_curve") else "")
    return "ok", ""


def _rub_state(lin: dict[str, Any], what: str) -> str:
    """``lineage.rates.status`` of a RUB curve; a run that did not record the statuses (before ``with_rates`` wrote
    them, ``rates._inferred_codes``): unknown."""
    st = (lin.get("rates") or {}).get("status")
    if not isinstance(st, dict):
        return "unknown"
    return "ok" if st.get(what) == "ok" else "missing"


def _fixings_state(lin: dict[str, Any], code: str) -> tuple[str, str]:
    """RUONIA / KEY_RATE (``lineage.rates.fixings``) or an overnight index of ``curves.fx`` (``lineage.fx.index_fixings``)."""
    r = ((lin.get("rates") or {}).get("fixings") or {}).get(code)
    if r is None:
        r = ((lin.get("fx") or {}).get("index_fixings") or {}).get(code)
    if not isinstance(r, dict):
        return ("unknown", "") if not lin.get("rates") and not lin.get("fx") else ("missing", "фиксингов индекса нет")
    if r.get("status") not in (None, "ok") or not r.get("rows", 1):
        return "missing", "фиксингов индекса нет"
    if r.get("unpublished_business_days"):
        return "stale", "последний фиксинг перенесён на непубликованные дни"
    return "ok", "; ".join(p for p in ((f"последний {r['latest']}" if r.get("latest") else ""),
                                     "дни без публикации — по предыдущему значению" if r.get("bridged_days") else "") if p)


INDEX_NEEDS = {"RUONIA": ("RUB_RUONIA_OIS", "ruonia_ois", "RUONIA"), "KEY_RATE": ("RUB_KEY_RATE_SWAP", "key_rate_swap", "KEY_RATE")}
"""A RUB index of a floating leg -> (its curve, its status in ``lineage.rates.status``, its fixings)."""


def _curve_name(cid: str, names: dict[str, str]) -> str:
    if cid in names:
        return names[cid]
    if cid == fxcurves.XCCY_CURVE_ID:
        return "CNY: XCCY к RUONIA"
    return f"{cid[:3]}: кривая валюты (FX implied)" if cid.endswith(" FX") else cid


def _curve_order(lin: dict[str, Any], ccy: str, ccs: bool) -> list[str]:
    """The curves a deal's ``ccy`` leg is valued on, the first one wanted, the rest its fallbacks, as
    ``valuation.derivatives`` takes them: an FX forward's by the run's setting ``fx_foreign_curve``
    (``FxMarket.fx_curve_ids``: the chosen kind, else the other), a CCS leg's the XCCY curve first."""
    opt = ((lin.get("fx") or {}).get("options") or {}).get("foreign_curve") or fxcurves.FxOptions().foreign_curve
    try:
        primary, alts = fxcurves.FxMarket().fx_curve_ids(ccy, fxcurves.FxOptions(foreign_curve=opt))
    except Exception:  # noqa: BLE001 - an unknown setting of an older run: the default order
        primary, alts = fxcurves.FxMarket().fx_curve_ids(ccy, fxcurves.FxOptions())
    first = [fxcurves.XCCY_CURVE_ID] if ccs and ccy == "CNY" else []
    return list(dict.fromkeys([*first, primary, *alts]))


def _foreign_curve(lin: dict[str, Any], ccy: str, ccs: bool, names: dict[str, str]) -> dict[str, Any]:
    """The foreign curve of a ``ccy`` leg: the wanted one with its state; only a fallback there: ``replaced``; none of
    them: ``missing``."""
    order = _curve_order(lin, ccy, ccs)
    for cid in order:
        st, why = _curve_state(lin, cid)
        if st == "missing":
            continue
        if cid == order[0]:
            return _need("curve", cid, _curve_name(cid, names), st, why)
        return _need("curve", cid, _curve_name(cid, names), "replaced",
                     f"вместо {order[0]}: её нет в рыночных данных прогона" + (f"; {why}" if why else ""))
    return _need("curve", order[0], _curve_name(order[0], names), "missing",
                 "в рыночных данных прогона нет ни одной из кривых: " + ", ".join(order))


def _index_needs(lin: dict[str, Any], t: dict[str, Any], code: str, trade_start: str | None, bd: date,
                 names: dict[str, str]) -> list[dict[str, Any]]:
    """What a floating leg needs, as ``valuation.derivatives`` reads it: the index's curve (projection), and its fixings
    only once a floating period has started (``start_date`` before the business date; not stated: needed) — a REPO's
    always. An index the valuation does not take for this kind of trade (RUSFAR but on a CCS's RUB leg, an unknown one)
    is one item «нет» with why, never «фиксинги — есть» beside a deal the run did not value for it."""
    kind = str(t.get("product_kind") or "")
    start = str(t.get("start_date") or trade_start or "")
    # the valuation compares the schedule's adjusted start (``derivatives._swap_instrument``: ``float_dates[0]``); the
    # terms' unadjusted start_date differs from it only when it falls on a holiday, and then by a day or two: building
    # the schedule here would need the run's calendars, which the API does not have
    started = kind == "repo" or not start or start < bd.isoformat()
    unsupported = _need("fixing", code, f"индекс {code}", "missing",
                        "индекс не поддерживается для этого вида сделки: в рыночных данных монитора его нет")
    rub_leg = _ccy(t.get("ccy")) in (None, "RUB")
    out: list[dict[str, Any]] = []
    if code in INDEX_NEEDS and (kind in ("repo", *dv.SWAP_KINDS) or (kind == "ccs" and rub_leg)):
        cid, status, fix = INDEX_NEEDS[code]
        if code == "KEY_RATE":
            curve = CURVE_NAMES.get(cid, cid)
            out.append(_need("curve", curve, names.get(curve, curve), _rub_state(lin, status), f"прогноз индекса {code}"))
        if started:
            out.append(_need("fixing", fix, f"фиксинги {code}", *_fixings_state(lin, fix)))
        return out
    if code == "RUSFAR" and kind == "ccs" and rub_leg:
        return [_need("fixing", "RUSFAR", "фиксинги RUSFAR", *_fixings_state(lin, "RUSFAR"))] if started else []
    if code in dv.CCS_FOREIGN_INDEX and kind == "ccs" and not rub_leg:
        fi = dv.CCS_FOREIGN_INDEX[code]
        out.append(_need("curve", fi.curve_id, _curve_name(fi.curve_id, names), *_curve_state(lin, fi.curve_id)))
        if started:
            out.append(_need("fixing", fi.fixing_id, f"фиксинги {code}", *_fixings_state(lin, fi.fixing_id)))
        return out
    return [unsupported]


def _needed(lin: dict[str, Any], terms: list[dict[str, Any]], x: dict[str, Any], bd: date,
            names: dict[str, str]) -> list[dict[str, Any]]:
    """What an unvalued derivative's terms need, each with its state in the run's market: the RUB curve; per foreign
    currency its spot and its curve (``_foreign_curve``); per floating index its curve and fixings; per NDF leg the CBR
    fixing of each foreign currency of its pair (a cross NDF fixes at the cross of both, TDM#616; one of the business
    date not yet published: pending, as the valuation projects it)."""
    out = [_need("curve", "RUB RUONIA", names.get("RUB RUONIA", "RUB RUONIA OIS"), _rub_state(lin, "ruonia_ois"),
                 "дисконтирование рублёвых потоков")]
    ccys = sorted({c for t in terms for k in ("base_ccy", "quote_ccy", "ccy", "settle_ccy") if (c := _ccy(t.get(k)))}
                  | ({c} if (c := _ccy(x.get("ccy"))) else set()))
    fx = lin.get("fx") or {}
    for c in ccys:
        if c == "RUB":
            continue
        spot = (fx.get("spots") or {}).get(c)
        out.append(_need("fx", c, f"курс {c}/RUB (спот)", "ok" if isinstance(spot, str) else "unknown" if not fx
                         else "missing", "" if isinstance(spot, str) or not fx else "курса нет в срезе хаба"))
        if not fx:
            out.append(_need("curve", fxcurves.implied_curve_id(c), _curve_name(fxcurves.implied_curve_id(c), names),
                             "unknown"))
            continue
        ccs = any(t.get("product_kind") == "ccs" and _ccy(t.get("ccy")) == c for t in terms)
        out.append(_foreign_curve(lin, c, ccs, names))
    starts = {}
    for t in terms:   # a trade's start: its legs' earliest ``start_date`` (None: not stated)
        if t.get("start_date"):
            tid = str(t.get("trade_id"))
            starts[tid] = min(starts.get(tid) or str(t["start_date"]), str(t["start_date"]))
    for t in terms:
        code = str(t.get("index_code") or "").strip().upper()
        if not code:
            continue
        out += _index_needs(lin, t, code, starts.get(str(t.get("trade_id"))), bd, names)
    fixed = set(fx.get("fixings") or ())
    for t in terms:
        if t.get("ndf") not in (True, "true", "True", 1, "1") or not t.get("fixing_date"):
            continue
        d = str(t["fixing_date"])
        for c in dict.fromkeys(c for k in ("base_ccy", "quote_ccy") if (c := _ccy(t.get(k))) and c != "RUB"):
            if d > bd.isoformat() or (d == bd.isoformat() and f"{c} {d}" not in fixed):
                out.append(_need("fixing", f"{c} {d}", f"фиксинг ЦБ {c} на {d}", "pending",
                                 "дата фиксинга после даты прогона" if d > bd.isoformat()
                                 else "фиксинг дня ещё не опубликован"))
            else:
                out.append(_need("fixing", f"{c} {d}", f"фиксинг ЦБ {c} на {d}",
                                 "ok" if f"{c} {d}" in fixed else "unknown" if not fx else "missing"))
    return _unique(out)


def _ndf_notes(terms: list[dict[str, Any]], ex: dict[str, Any]) -> list[str]:
    """TDM#616 in the market notes: why a derivative valued without core has no rates risk (``extra.risk_missing``, the
    words of ``valuation.positions.RISK_MISSING``), and the settlement currency taken for an NDF whose terms name none
    (the valued legs' ``settlement_ccy_assumed``; terms without ``settle_ccy`` settle in RUB, or the adapter's default
    it marked ``x_settle_ccy_assumed``)."""
    out = []
    rm = ex.get("risk_missing")
    if isinstance(rm, dict) and rm.get("reason") in RISK_MISSING:
        ccy = rm.get("ccy") if isinstance(rm.get("ccy"), str) else ""
        out.append("процентного риска нет: " + RISK_MISSING[rm["reason"]].format(ccy=ccy))
    taken = {str(lg.get("settlement_ccy") or "RUB") for lg in ex.get("legs") or ()
             if isinstance(lg, dict) and lg.get("settlement_ccy_assumed")}
    for t in terms:
        if t.get("ndf") in (True, "true", "True", 1, "1"):
            c = _ccy(t.get("settle_ccy"))
            if c is None or t.get("x_settle_ccy_assumed") in (True, "true", "True", 1, "1"):
                taken.add(c or "RUB")
    out += [f"NDF: валюта расчёта не указана в условиях — принята {c}" for c in sorted(taken)]
    return out


def _unique(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first item of each (kind, id)."""
    seen: set[tuple[str, str]] = set()
    out = []
    for n in items:
        if (n["kind"], n["id"]) not in seen:
            seen.add((n["kind"], n["id"]))
            out.append(n)
    return out


REPORT_QUALITIES = ("reported_value", "assumed")
"""Qualities of a value taken from the report (or assumed), not from market data."""


def _report_valued(fact: dict[str, Any]) -> bool:
    """A value not core's and not from a quote: a row other than a bond valued at the report's figure, or a bond not
    matched to the hub valued from the report (``publish.tdm_run.direct_row``: no ``price_basis``) or assumed."""
    ex = fact.get("extra") or {}
    if ex.get("core") or ex.get("price_basis"):
        return False
    return fact.get("instr_type") != "BOND" or (ex.get("quality") or fact.get("quality")) in REPORT_QUALITIES


def _used(lin: dict[str, Any], fact: dict[str, Any], names: dict[str, str]) -> list[dict[str, Any]]:
    """What a valued position's valuation used, from its fact: a derivative's curves (a fallback: replaced), FX
    rates and fixings; a bond's quote and curves (a proxy curve: replaced)."""
    ex = fact.get("extra") or {}
    core = ex.get("core") or {}
    out: list[dict[str, Any]] = []
    if core:
        rub = core.get("rub_curve")
        if rub:
            out.append(_need("curve", rub, names.get(rub, rub), "ok", "дисконтирование рублёвых потоков"))
        fb = set(core.get("curve_fallback_from") or ())
        used = [CURVE_NAMES.get(c, c) for c in core.get("curves") or core.get("foreign_curves") or ()]
        product = str(core.get("product") or "")
        ccs, fxp = "ccs" in product, "fx_" in product
        for c in used:
            st, why = _curve_state(lin, c) if c in ((lin.get("fx") or {}).get("curves") or {}) else ("ok", "")
            # the curve that stood in: of the currency of a curve that fell back (``FxMarket.fx_curve_ids``), or not
            # the curve the valuation takes first for its currency (a CCS: XCCY; an FX forward: by the setting) while
            # that one was not used: the run did not record every fallback (a CCS's foreign curve)
            instead = sorted(f for f in fb if f != c and f[:4] == c[:4])
            if not instead and (ccs or fxp) and _ccy(c[:3]) and c[:3] != "RUB":
                order = _curve_order(lin, c[:3], ccs)
                if c in order and c != order[0] and order[0] not in used:
                    instead = [order[0]]
            out.append(_need("curve", c, _curve_name(c, names), "replaced" if instead else st,
                             ("вместо " + ", ".join(instead) + " (её нет в срезе)") if instead else why))
        for c in sorted(fb):
            out.append(_need("curve", c, names.get(c, c), "missing", "кривой нет в срезе: оценка на запасной"))
        for c, rate in sorted((ex.get("fx_rates") or {}).items()):
            out.append(_need("fx", c, f"курс {c}/RUB (спот)", "ok" if rate is not None else "missing"))
        for fid, info in sorted((ex.get("index_fixings") or {}).items()):
            late = isinstance(info, dict) and info.get("unpublished_business_days")
            out.append(_need("fixing", fid, f"фиксинги {fid}", "stale" if late else "ok",
                             "последний фиксинг перенесён на непубликованные дни" if late else ""))
        for f in ex.get("fixings") or ():
            st = {"fixed": "ok", "pending": "pending", "open": "pending"}.get(f.get("status"), "unknown")
            out.append(_need("fixing", f"{f.get('trade_id')} {f.get('fixing_date')}",
                             f"фиксинг NDF на {f.get('fixing_date')}", st, f.get("source") or ""))
        return out
    q = ex.get("quality") or fact.get("quality")
    if _report_valued(fact):
        # a row valued at the report's figure (or assumed): its curves are the report's risk, not data the run used
        if ex.get("fx_rate") is not None and fact.get("ccy") not in (None, "RUB"):
            return [_need("fx", fact["ccy"], f"курс {fact['ccy']}/RUB", "ok", "пересчёт стоимости отчёта в рубли")]
        return []
    # a bond: its quote, its curves
    st, why = {"stale_price": ("stale", "котировки дня нет: цена прежнего дня"),
               "model_price": ("replaced", "котировки нет: модельная цена"),
               "par_proxy": ("replaced", "котировки нет: по номиналу"),
               "reported_value": ("replaced", "котировки нет: стоимость из отчёта позиций")}.get(q, ("ok", ""))
    if st == "ok" and ex.get("price_basis"):
        why = f"цена: {ex['price_basis']}"
    out.append(_need("quote", "quote", "котировка бумаги", st, why))
    proxy = ex.get("curve_proxy") or {}
    for c in (fact.get("risk") or {}):
        if proxy.get("curve") == c:
            out.append(_need("curve", c, names.get(c, c), "replaced",
                             f"вместо {proxy.get('instead_of')}" + (f": {proxy['text']}" if proxy.get("text") else "")))
        else:
            out.append(_need("curve", c, names.get(c, c), "ok"))
    if ex.get("fx_rate") is not None and fact.get("ccy") not in (None, "RUB"):
        out.append(_need("fx", fact["ccy"], f"курс {fact['ccy']}/RUB", "ok"))
    return out


def _market(con, run: dict[str, Any], kind: str, fact: dict[str, Any] | None, terms: list[dict[str, Any]],
            x: dict[str, Any], reason: str | None, fallback: str | None = None,
            got: dict[str, Any] | None = None) -> dict[str, Any]:
    """``fact``: the valuation's (a derivative only when core valued it); ``fallback``: the quality of the value an
    unvalued derivative was given instead (the report's, or 0), said in a note; ``got``: the ``extra`` of the fact the
    position got either way (its ``risk_missing``, its legs: ``_ndf_notes``)."""
    lin = run.get("lineage") or {}
    ref = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'curves'",
                      (run["run_id"],)).fetchone()
    names = {c["curve_id"]: c.get("name") or c["curve_id"] for c in ((ref or {}).get("data") or [])
             if isinstance(c, dict) and c.get("curve_id")}
    notes = [f"в срезе хаба устарели данные «{k}»" + (f": {'; '.join(str(r) for r in v[:3])}" if v else "")
             for k, v in sorted((lin.get("stale_datasets") or {}).items())]
    notes = _ndf_notes(terms, got or {}) + notes
    if fact is not None:
        items = _unique(_used(lin, fact, names))
        ex = fact.get("extra") or {}
        if _report_valued(fact):
            q = ex.get("quality") or ""
            notes.insert(0, "стоимость: " + cv.APPROX_TEXT.get(q, q or "из отчёта позиций"))
            return {"basis": "used" if items else "none", "text": cv.PROVENANCE_TEXT["market_reported"],
                    "items": items, "notes": notes}
        if not items:
            return {"basis": "unknown", "text": cv.PROVENANCE_TEXT["market_unknown"], "items": [], "notes": notes}
        return {"basis": "used", "text": cv.PROVENANCE_TEXT["market_used"], "items": items, "notes": notes}
    if fallback:
        notes.insert(0, "оценка не по условиям сделки: " + cv.APPROX_TEXT.get(fallback, fallback))
    if reason:
        notes.insert(0, "почему не оценена: " + cv.why_of(reason, x, None)["why"])
    if kind == "derivative" and (terms or x.get("ccy")):
        if not lin.get("rates") and not lin.get("fx"):
            return {"basis": "unknown", "text": cv.PROVENANCE_TEXT["market_unknown"], "items": [], "notes": notes}
        if not terms:
            notes.insert(0, cv.PROVENANCE_TEXT["needed_no_terms"])
        return {"basis": "needed", "text": cv.PROVENANCE_TEXT["market_needed"],
                "items": _needed(lin, terms, x, run["business_date"], names), "notes": notes}
    if kind != "derivative":   # not valued at all: no market data was used
        return {"basis": "none", "text": cv.PROVENANCE_TEXT["market_not_valued"], "items": [], "notes": notes}
    return {"basis": "unknown", "text": cv.PROVENANCE_TEXT["market_unknown"], "items": [], "notes": notes}


# ---- the position -----------------------------------------------------------------------------------------------

def _kind(x: dict[str, Any], fact: dict[str, Any] | None, bd: date, off: bool) -> str:
    """``bond``, ``derivative`` (valued from its terms), ``other`` (a spot FX row, a future, ...) or ``linked`` (a row
    without ``asset_class`` but with a ``deal_id``: a derivative when terms link to it, else a bond, as
    ``valuation.positions``)."""
    if off or ((fact or {}).get("extra") or {}).get("core"):
        return "derivative"
    cls = str(x.get("asset_class") or "").lower()
    if cls in TERMS_KINDS:
        return "derivative"
    if not cls and x.get("deal_id"):   # a derivative only when terms name it (TDM#501), else a bond
        return "linked"
    if cls == "fx":
        vd = x.get("value_date")
        vd = date.fromisoformat(vd) if isinstance(vd, str) else vd
        nr = str(((fact or {}).get("extra") or {}).get("not_exact_reason") or "")
        return "derivative" if (vd is not None and vd > bd) or nr.startswith("no_trade_terms") else "other"
    if cls == "bond" or (fact or {}).get("instr_type") == "BOND" or not cls:
        return "bond"
    return "other"


def _future(x: dict[str, Any], reason: str | None) -> bool:
    """The report row is an FX future (``concepts.instr_kind``: its product group), or the run's reason names the future
    it took the row for (``valuation.positions._future_row``: ``future <ticker>: ...``)."""
    rep = x.get("x") if isinstance(x.get("x"), dict) else {}
    kind = concepts.instr_kind({"ccy": x.get("ccy"), "asset_class": x.get("asset_class"), "extra": {"reported": rep}})
    detail = str(reason or "").partition(":")[2].strip()
    return kind == "FX_FUT" or detail.startswith("future ") or str(reason or "").startswith("future_")


def _mismatch_text(code: str, rows: int, reason: str | None) -> str:
    """``coverage.MISMATCH_TEXT``; one deal on several rows: why its terms are not valued only when that is the run's
    reason (``terms_ambiguous``), and how many rows when known."""
    text = cv.MISMATCH_TEXT[code]
    if code == "deal_on_rows":
        if str(reason or "").startswith("terms_ambiguous"):
            text += ": " + cv.PROVENANCE_TEXT["deal_on_rows_terms"]
        if rows > 1:
            text += f" (строк: {rows})"
    return text


def provenance(con, run: dict[str, Any], pid: str, feeds: list[dict[str, Any]] | None = None,
               history: dict[str, set[str]] | None = None) -> dict[str, Any] | None:
    """The module doc's answer for position ``pid`` of ``run``; None: no such position in the run. ``feeds``: the
    adapter services' feeds (``adapter_services.read_state``), ``history``: the marks of the fetch of past dates
    (``trades.history_files``), for where a trade's file came from."""
    p = con.execute("SELECT position_id, book_id, secid, quality, reason, fact, pnl, no_pnl FROM run_position"
                    " WHERE run_id = %s AND position_id = %s", (run["run_id"], pid)).fetchone()
    if p is None:
        return None
    book, fact = p["book_id"], p["fact"]
    lin = run.get("lineage") or {}
    bd = run["business_date"]
    ex = (fact or {}).get("extra") or {}
    off_list = (((lin.get("positions") or {}).get("off_report") or {}).get("list") or [])
    off_trade = None
    if ex.get("off_report") or any(f"{t.get('book')}|{t.get('trade_id')}" == pid for t in off_list):
        off_trade = pid.split("|", 1)[1] if "|" in pid else str((ex.get("reported") or {}).get("deal_id") or pid)
    rep_src, row, same = _report(con, run, pid, book)
    x = {**(ex.get("reported") or {}), **(row or {})}
    kind = _kind(x, fact, bd, off_trade is not None)
    got, sets, known = _terms(con, run, pid, book, x, fact, off_trade) if kind != "bond" else ([], {}, True)
    if kind == "linked":
        kind = "derivative" if got else "bond"
    elif kind == "other" and got:   # any non-bond row with terms is valued from them (``valuation.positions``)
        kind = "derivative"
    # a derivative is valued only by core from its terms; a fact without core is the value it got instead
    # (``valuation.positions._not_by_core``: the report's MV, or 0 for an FX forward): not valued
    by_core = fact is not None and (kind != "derivative" or bool(ex.get("core")))
    reason = p.get("reason") or (None if by_core else ex.get("not_exact_reason"))
    pos = lin.get("positions") or {}
    any_units = any(str((u.get("scope") or {}).get("book_id")) == book for u in pos.get("trade_terms") or ())
    # an FX future valued by its exchange ticker (setting ``futures_by_ticker``): no terms to have
    by_ticker = "fx_future" in str((ex.get("core") or {}).get("product") or "") or (not got and _future(x, reason))
    terms_src = _terms_source(kind, got, sets, known, any_units or bool(got), by_ticker)
    ids = {str(v) for v in (x.get("deal_id"), off_trade) if v} | {str(r.get("trade_id")) for r in got if r.get("trade_id")}
    for col in ("pnl", "no_pnl"):   # the trades the run put in this position (``pnl.new_trades``)
        block = p.get(col) or {}
        for t in (block.get("extra") or {}).get("trades") or block.get("trades") or ():
            if isinstance(t, dict) and t.get("trade_id") is not None:
                ids.add(str(t["trade_id"]))
    trades_src, n_trades = _trades(con, run, pid, book, ids, feeds, history)
    ccys = sorted({c for c in (_ccy(x.get("ccy")), _ccy((fact or {}).get("ccy")),
                               *(_ccy(k) for k in ((fact or {}).get("fx_delta") or {})),
                               *(_ccy(t.get(k)) for t in got for k in ("base_ccy", "quote_ccy", "ccy"))) if c})
    mism = []
    if row is not None and kind == "derivative" and not got and known and not by_ticker:
        mism.append("report_no_terms")
    if row is None and n_trades and not got:
        mism.append("trades_no_report")
    if row is None and got:
        mism.append("terms_no_report")
    # several report rows of one deal_id are one deal on several rows unless each takes its own leg (an FX swap given
    # as a row per leg, the legs naming their positions): the run's terms_ambiguous, or no leg names this position
    if str(reason or "").startswith("terms_ambiguous") or (
            same > 1 and not any(str(t.get("position_id") or "") == pid for t in got)):
        mism.append("deal_on_rows")
    gap = ((ex.get("reported") or {}).get("x") or {}).get("trades_report_gap")   # the run's, not the stored row's
    if gap is not None:
        mism.append("report_trades_gap")
    return {
        "run_id": run["run_id"], "business_date": bd.isoformat(), "position_id": pid, "book_id": book, "kind": kind,
        "valued": by_core, "off_report": off_trade is not None,
        "sources": [rep_src, terms_src, trades_src, *_book_ccy(con, run, book, ccys)],
        "mismatches": [{"code": m, "text": _mismatch_text(m, same, reason)} for m in mism],
        "market": _market(con, run, kind, fact if by_core else None, got, x, reason,
                          None if by_core or fact is None else (ex.get("quality") or p.get("quality")), ex),
    }
