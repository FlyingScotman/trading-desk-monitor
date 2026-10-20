"""Instruments export: the bond universe of a static snapshot as the desk sees it, without positions (R9).

``desk instruments export --out instruments.csv`` (or ``.xlsx``) writes one row per instrument of a cached static
snapshot (public hub data only; nothing from the desk store or the private inputs):

- the hub facts (Market Snapshot Contract v1 ``instruments``, ``issuers``, ``coupon_periods``, ``redemptions``,
  ``offers``, ``floater_formulas``), MOEX labels verbatim;
- what the monitor would show for a position in it (``mon_*``: the name, issuer, issuer group, sector, rating,
  asset class, coupon type and current coupon as ``publish.tdm_run`` and ``api.compat`` build them);
- whether core can value it from the static data alone (``core_static``: ``ok`` or the stable reason code of
  ``valuation.bonds``, the same checks a position goes through before core) and the assumptions it would carry;
  with ``--value`` and a market snapshot of the date, the full valuation's reason (quote, price, core);
- data-quality flags (``dq_flags``, codes in :data:`FLAGS`).

The coverage summary (:func:`summary`) counts fills per field and bond class, the reasons and the flags; the
static data audit (``docs/architecture/reviews/STATIC_DATA_AUDIT_2026-10-20.md``) is built from it.

CSV is UTF-8 with a BOM and a comma separator (the CSV module's quoting); ``.xlsx`` is written with the standard
library (sheets ``instruments``, ``summary``, ``columns``) and opens in Excel without import settings.
"""

from __future__ import annotations

import csv
import io
import re
import statistics
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from ..marketdata.adapter import BondTerms, MarketView, StaticView
from ..marketdata.bundle import Snapshot, SnapshotDir, SnapshotError, read_bundle

# What the monitor shows (publish.tdm_run): kept in step by test_instruments_export.
MON_ASSET_CLASS = {"ofz_pd": "ОФЗ-ПД", "ofz_ad": "ОФЗ-АД", "ofz_pk": "ОФЗ-ПК", "ofz_in": "ОФЗ-ИН",
                   "subfederal": "Субфедеральные", "municipal": "Муниципальные", "corporate": "Корпоративные"}
MON_SEGMENT = {"ofz_pd": "Гос", "ofz_ad": "Гос", "ofz_pk": "Гос", "ofz_in": "Гос", "subfederal": "Суб",
               "municipal": "Суб"}

COLUMNS: tuple[tuple[str, str], ...] = (
    ("secid", "SECID MOEX (ключ инструмента в мониторе)"),
    ("isin", "ISIN"),
    ("regnumber", "Номер госрегистрации"),
    ("status", "Статус в хабе: active / matured"),
    ("name", "Полное наименование выпуска (MOEX NAME)"),
    ("short_name", "Краткое наименование (MOEX SHORTNAME, до 10 символов)"),
    ("mon_name", "Как бумага названа в мониторе (MOEX NAME, D1; краткое имя и SECID остаются кодами)"),
    ("issuer_id", "ID эмитента MOEX (emitent_id)"),
    ("issuer_name", "Эмитент: полное наименование (MOEX emitent_title)"),
    ("issuer_short_name", "Эмитент: краткое наименование (в хабе не заполняется)"),
    ("issuer_inn", "ИНН эмитента"),
    ("issuer_ogrn", "ОГРН эмитента"),
    ("issuer_country", "Страна эмитента"),
    ("issuer_is_sovereign", "Суверенный эмитент (Минфин РФ)"),
    ("mon_issuer", "Эмитент в мониторе: краткое, иначе полное наименование без ОПФ и кавычек (D2)"),
    ("mon_issuer_group", "Группа эмитента в мониторе (пока в хабе нет групп, HS4: = эмитент в мониторе)"),
    ("sector", "Отрасль (issuers.sector хаба)"),
    ("rating", "Рейтинг (в контракте хаба поля нет)"),
    ("bond_class", "Класс бумаги хаба (bond_class)"),
    ("mon_asset_class", "Класс актива в мониторе"),
    ("mon_segment", "Сегмент в мониторе"),
    ("sec_type", "MOEX TYPE"),
    ("bond_type", "MOEX BOND_TYPE"),
    ("bond_subtype", "MOEX BOND_SUBTYPE"),
    ("coupon_details", "MOEX COUPON_DETAILS"),
    ("coupon_type", "Тип купона хаба: fixed / floating / indexed_face / zero / variable_known / unknown"),
    ("coupon_type_basis", "Основание типа купона (moex_label, schedule, schedule_constant, ofz_series, ...)"),
    ("mon_coupon_type", "Тип купона в мониторе (fix / float / zero / indexed_face / variable_known / unknown, D5)"),
    ("coupon_frequency", "Купонов в год (MOEX COUPONFREQUENCY)"),
    ("coupon_period_days", "Длина купонного периода, дней (MOEX COUPONPERIOD)"),
    ("implied_frequency", "Купонов в год по графику (медиана периодов)"),
    ("coupons_total", "Купонов в графике"),
    ("coupons_future", "Будущих купонов"),
    ("coupons_future_no_value", "Будущих купонов без суммы и ставки"),
    ("coupon_status_mix", "Статусы будущих купонов: known/current_published/projected_by_moex/unknown"),
    ("current_coupon_no", "Текущий купон: номер"),
    ("current_coupon_start", "Текущий купон: начало"),
    ("current_coupon_pay_date", "Текущий купон: дата выплаты"),
    ("current_coupon_rate_pct", "Текущий купон: ставка, % годовых (MOEX valueprc)"),
    ("current_coupon_amount", "Текущий купон: сумма на бумагу"),
    ("current_coupon_status", "Текущий купон: статус хаба"),
    ("mon_coupon_pct", "Купон, который покажет монитор (D4): ставка текущего купона, иначе его сумма как ставка, иначе последняя известная ставка; у флоатера без суммы монитор покажет прогноз «индекс + маржа» по фиксингу"),
    ("coupon_benchmark", "MOEX COUPON_BENCHMARK"),
    ("coupon_benchmark_spread_pct", "MOEX COUPON_BENCHMARK_SPREAD, п.п."),
    ("floater_benchmark", "Формула флоатера: индекс (хаб)"),
    ("floater_spread_bp", "Формула флоатера: спред, б.п."),
    ("floater_status", "Формула флоатера: curated / inferred / partial"),
    ("floater_lag_days", "Формула флоатера: лаг, дней"),
    ("floater_averaging", "Формула флоатера: усреднение"),
    ("floater_floor_cap_pct", "Формула флоатера: floor / cap, %"),
    ("face_unit", "Валюта номинала"),
    ("currency", "Валюта расчётов (основной режим)"),
    ("face_value", "Текущий номинал"),
    ("initial_face_value", "Первоначальный номинал"),
    ("issue_date", "Дата размещения"),
    ("maturity_date", "Дата погашения"),
    ("is_perpetual", "Бессрочная"),
    ("is_amortizing", "Амортизация"),
    ("amortizations_future", "Будущих амортизационных выплат"),
    ("redemptions_future_sum", "Сумма будущих погашений на бумагу"),
    ("next_offer_date", "Ближайшая оферта: дата"),
    ("next_offer_type", "Ближайшая оферта: тип (put / call / unknown)"),
    ("offers_scheduled", "Запланированных оферт"),
    ("is_subordinated", "Субординированная"),
    ("is_structured", "Структурная"),
    ("is_face_indexed", "Индексируемый номинал"),
    ("is_fx_face", "Валютный номинал"),
    ("qualified_only", "Только для квал. инвесторов"),
    ("high_risk", "Повышенный риск (MOEX HIGHRISK)"),
    ("listing_level", "Уровень листинга"),
    ("issue_size", "Объём выпуска, бумаг"),
    ("primary_board", "Основной режим торгов"),
    ("day_count", "База расчёта (day count; null = не публикуется)"),
    ("labels", "Метки покрытия хаба"),
    ("core_static", "Оценка ядром по статике: ok или код причины (valuation.bonds)"),
    ("core_static_detail", "Подробности причины"),
    ("core_assumptions", "Допущения, с которыми ядро оценит бумагу"),
    ("has_quote", "Есть котировка в рыночном снапшоте даты"),
    ("price_pct", "Цена, % (legal_close, close, waprice)"),
    ("num_trades", "Сделок за день"),
    ("core_market", "--value: оценка ядром с котировкой: exact / assumed или код причины"),
    ("core_market_detail", "--value: подробности причины или допущения"),
    ("dq_flags", "Флаги качества данных (коды, через ;)"),
)

FLAGS: dict[str, str] = {
    "class_unknown": "bond_class = unknown",
    "coupon_type_unknown": "тип купона не определён",
    "fixed_label_unknown_coupons": "MOEX: «фиксированный с известными купонами», но будущие купоны без значения",
    "coupon_schedule_empty": "нет ни одного купона в графике (не дисконтная)",
    "current_coupon_unknown": "у текущего купонного периода нет ни ставки, ни суммы",
    "future_coupon_gaps": "будущие купоны без суммы и ставки (не флоатер)",
    "frequency_missing": "нет COUPONFREQUENCY у купонной бумаги",
    "frequency_mismatch": "COUPONFREQUENCY расходится с графиком купонов",
    "floater_formula_missing": "флоатер без формулы",
    "floater_formula_partial": "формула флоатера без спреда",
    "floater_formula_inferred": "формула флоатера выведена (лаг, усреднение, floor/cap не известны)",
    "floater_benchmark_unsupported": "индекс флоатера не поддерживается ядром",
    "maturity_missing": "нет даты погашения у не-бессрочной бумаги",
    "perpetual_no_call": "бессрочная без даты колла",
    "redemption_mismatch": "сумма будущих погашений не равна номиналу",
    "redemption_amount_missing": "будущее погашение без суммы",
    "face_missing": "нет номинала",
    "offer_expected_none_scheduled": "подтип MOEX с офертой, но нет запланированной оферты",
    "offer_date_missing": "оферта без даты",
    "currency_mismatch": "валюта номинала и валюта расчётов различаются",
    "isin_missing": "нет ISIN (позиции банка сопоставляются по ISIN)",
    "isin_shared": "ISIN у нескольких SECID (сопоставление неоднозначно)",
    "issuer_missing": "нет эмитента",
    "issuer_name_dated": "наименование эмитента в старой форме (ОАО / ЗАО / открытое акционерное общество)",
    "name_latin": "краткое наименование без кириллицы",
    "matured": "погашена (в снапшоте ещё 31 день)",
}

FILL_FIELDS = ("isin", "regnumber", "name", "short_name", "issuer_name", "issuer_short_name", "issuer_inn",
               "issuer_ogrn", "issuer_country", "sector", "rating", "coupon_frequency", "current_coupon_rate_pct",
               "mon_coupon_pct", "floater_benchmark", "floater_spread_bp", "floater_lag_days", "face_value",
               "maturity_date", "issue_date", "listing_level", "issue_size", "day_count")

_DATED_FORM = re.compile(r"(^|[\s\"«])(ОАО|ЗАО|открыт\w* акционерн\w*|закрыт\w* акционерн\w*)", re.IGNORECASE)
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


@dataclass
class Export:
    static: Snapshot
    market: Snapshot | None
    rows: list[dict[str, Any]]


def _s(v: Any) -> str | None:
    """Wire value as text: decimals stay their exact digits, booleans 0/1, lists joined."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (list, tuple)):
        return ";".join(str(x) for x in v) or None
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v.to_integral() else str(v)
    return str(v)


# ------------------------------------------------------------------------------ snapshot choice

def find_snapshots(snapshots: Path, on: str | None = None, static_file: Path | None = None,
                   market_file: Path | None = None) -> tuple[Snapshot, Snapshot | None]:
    """The static snapshot to export and the market snapshot of its date (None when there is none).

    ``static_file``: that bundle. Otherwise from the cache folder: with ``on``, the static snapshot the
    ``eod_final`` market snapshot of ``on`` names (what ``desk eod`` would use), else the newest static snapshot
    dated on or before ``on``; without ``on``, the newest static snapshot."""
    if static_file is not None:
        static = read_bundle(Path(static_file))
        market = read_bundle(Path(market_file)) if market_file else None
        return static, market
    sd = SnapshotDir(Path(snapshots))
    manifests = sd.manifests()
    market = None
    if on is not None:
        try:
            market = sd.market(on)
        except SnapshotError:
            market = None
        if market is not None and market.static_snapshot_id and market.static_snapshot_id in manifests:
            return sd.get(market.static_snapshot_id), market
    statics = sorted((m["business_date"], sid) for sid, m in manifests.items()
                     if m["kind"] == "static" and (on is None or m["business_date"] <= on))
    if not statics:
        raise SnapshotError(f"no static snapshot in {snapshots}" + (f" dated on or before {on}" if on else ""))
    static = sd.get(statics[-1][1])
    if market is None:
        try:
            market = sd.market(static.business_date)
        except SnapshotError:
            market = None
    return static, market


# ------------------------------------------------------------------------------------- rows

def _periods(t: BondTerms) -> list[tuple[Any, date | None]]:
    """(coupon, start) with the start filled from the previous payment."""
    out, prev = [], None
    for c in t.coupons:
        out.append((c, c.start or prev))
        prev = c.pay_date
    return out


def _implied_frequency(t: BondTerms) -> int | None:
    days = [(c.pay_date - s).days for c, s in _periods(t) if s is not None and c.pay_date > s]
    if not days:
        return None
    med = statistics.median(days)
    return max(1, round(365 / med)) if med > 0 else None


def _static_check(t: BondTerms, as_of: date) -> tuple[str, str, str, float | None]:
    """(core_static, detail, assumptions, monitor coupon %) from the backend's own pre-core checks."""
    from ..valuation.bonds import Rejected, fixed_schedule  # the valuation layer, read-only

    try:
        s = fixed_schedule(t, as_of)
    except Rejected as e:
        return e.reason, e.detail, "", None
    except Exception as e:  # noqa: BLE001 - a crash in the checks is reported per bond, not fatal
        return f"error_{type(e).__name__}", str(e)[:300], "", None
    return "ok", "", "; ".join(s.assumptions), s.current_coupon_pct


def instrument_row(raw: dict[str, Any], t: BondTerms, view: StaticView, as_of: date,
                   shared_isins: dict[str, int], issuers: dict[int, dict[str, Any]]) -> dict[str, Any]:
    iss = view.issuer_of(t)
    iraw = issuers.get(t.issuer_id, {}) if t.issuer_id is not None else {}
    f = t.floater_formula or {}
    periods = _periods(t)
    future = [c for c, _ in periods if c.pay_date > as_of]
    no_value = [c for c in future if c.rate_pct is None and not c.amount_per_bond]
    current = next(((c, s) for c, s in periods if s is not None and s < as_of <= c.pay_date), None)
    if current is None:
        current = next(((c, s) for c, s in periods if c.pay_date > as_of), None)
    cur, cur_start = current if current else (None, None)
    reds = [r for r in t.redemptions if r.date > as_of]
    offers = sorted((o for o in t.offers if o.status == "scheduled" and o.date and o.date > as_of),
                    key=lambda o: o.date)  # type: ignore[arg-type,return-value]
    core, detail, assumptions, mon_coupon = _static_check(t, as_of)
    from ..publish.tdm_run import COUPON_TYPES, issuer_display  # what the monitor shows, from the same code
    from ..valuation.bonds import coupon_view

    mon_coupon = cv if (cv := coupon_view(t, as_of).pct) is not None else mon_coupon
    mon_issuer = issuer_display(iss.short_name or iss.name) if iss else f"issuer:{t.issuer_id or t.secid}"
    row: dict[str, Any] = {
        "secid": t.secid, "isin": t.isin, "regnumber": raw.get("regnumber"), "status": t.status,
        "name": raw.get("name"), "short_name": t.short_name, "mon_name": t.name or t.short_name,
        "issuer_id": t.issuer_id,
        "issuer_name": iss.name if iss else None, "issuer_short_name": iss.short_name if iss else None,
        "issuer_inn": iss.inn if iss else None,
        "issuer_ogrn": iraw.get("ogrn"), "issuer_country": iraw.get("country"),
        "issuer_is_sovereign": iss.is_sovereign if iss else None,
        "mon_issuer": mon_issuer, "mon_issuer_group": mon_issuer,
        "sector": iss.sector if iss else None, "rating": None,
        "bond_class": t.bond_class, "mon_asset_class": MON_ASSET_CLASS.get(t.bond_class, t.bond_class),
        "mon_segment": MON_SEGMENT.get(t.bond_class, "Корп"),
        "sec_type": raw.get("sec_type"), "bond_type": raw.get("bond_type"), "bond_subtype": raw.get("bond_subtype"),
        "coupon_details": raw.get("coupon_details"), "coupon_type": t.coupon_type,
        "coupon_type_basis": raw.get("coupon_type_basis"), "mon_coupon_type": COUPON_TYPES.get(t.coupon_type, t.coupon_type),
        "coupon_frequency": t.coupon_frequency, "coupon_period_days": raw.get("coupon_period_days"),
        "implied_frequency": _implied_frequency(t), "coupons_total": len(t.coupons), "coupons_future": len(future),
        "coupons_future_no_value": len(no_value),
        "coupon_status_mix": "/".join(str(sum(c.status == s for c in future))
                                      for s in ("known", "current_published", "projected_by_moex", "unknown")),
        "current_coupon_no": cur.no if cur else None, "current_coupon_start": cur_start,
        "current_coupon_pay_date": cur.pay_date if cur else None,
        "current_coupon_rate_pct": cur.rate_pct if cur else None,
        "current_coupon_amount": cur.amount_per_bond if cur else None,
        "current_coupon_status": cur.status if cur else None,
        "mon_coupon_pct": None if mon_coupon is None else round(mon_coupon, 4),
        "coupon_benchmark": raw.get("coupon_benchmark"),
        "coupon_benchmark_spread_pct": raw.get("coupon_benchmark_spread_pct"),
        "floater_benchmark": f.get("benchmark"), "floater_spread_bp": f.get("spread_bp"),
        "floater_status": f.get("status"), "floater_lag_days": f.get("lag_days"),
        "floater_averaging": f.get("averaging"),
        "floater_floor_cap_pct": (f"{f.get('floor_pct') or ''}/{f.get('cap_pct') or ''}"
                                  if f.get("floor_pct") or f.get("cap_pct") else None),
        "face_unit": t.face_unit, "currency": t.currency, "face_value": t.face_value,
        "initial_face_value": t.initial_face_value, "issue_date": raw.get("issue_date"),
        "maturity_date": t.maturity_date, "is_perpetual": t.is_perpetual, "is_amortizing": raw.get("is_amortizing"),
        "amortizations_future": sum(r.kind == "amortization" for r in reds),
        "redemptions_future_sum": sum((r.value_per_bond for r in reds if r.value_per_bond), Decimal(0)) if reds else None,
        "next_offer_date": offers[0].date if offers else None, "next_offer_type": offers[0].type if offers else None,
        "offers_scheduled": len(offers),
        "is_subordinated": raw.get("is_subordinated"), "is_structured": raw.get("is_structured"),
        "is_face_indexed": raw.get("is_face_indexed"), "is_fx_face": raw.get("is_fx_face"),
        "qualified_only": raw.get("qualified_only"), "high_risk": raw.get("high_risk"),
        "listing_level": raw.get("listing_level"), "issue_size": raw.get("issue_size"),
        "primary_board": t.primary_board, "day_count": raw.get("day_count"), "labels": list(t.labels),
        "core_static": core, "core_static_detail": detail, "core_assumptions": assumptions,
        "has_quote": None, "price_pct": None, "num_trades": None, "core_market": None, "core_market_detail": None,
    }
    row["dq_flags"] = flags(row, t, raw, as_of, shared_isins, no_value, cur)
    return row


def flags(row: dict[str, Any], t: BondTerms, raw: dict[str, Any], as_of: date, shared_isins: dict[str, int],
          no_value: list, cur) -> list[str]:
    out: list[str] = []
    active = t.status == "active"
    zero = t.coupon_type == "zero"
    floating = t.coupon_type == "floating"
    if t.bond_class == "unknown":
        out.append("class_unknown")
    if t.coupon_type == "unknown":
        out.append("coupon_type_unknown")
    if t.coupon_type == "variable_known" and raw.get("coupon_type_basis") == "schedule" and (
            raw.get("coupon_details") == "Фиксированный с известными купонами"
            or (raw.get("bond_type") or "").startswith("Облигация с фиксированным (известным)")):
        out.append("fixed_label_unknown_coupons")
    if not t.coupons and not zero:
        out.append("coupon_schedule_empty")
    if active and not zero and cur is not None and cur.rate_pct is None and not cur.amount_per_bond:
        out.append("current_coupon_unknown")
    if active and no_value and not floating and t.coupon_type != "unknown":
        out.append("future_coupon_gaps")
    if active and not zero and t.coupons and not t.coupon_frequency:
        out.append("frequency_missing")
    imp = row["implied_frequency"]
    if t.coupon_frequency and imp and abs(imp - t.coupon_frequency) > max(1, t.coupon_frequency // 4):
        out.append("frequency_mismatch")
    if floating:
        f = t.floater_formula
        if f is None:
            out.append("floater_formula_missing")
        else:
            if f.get("spread_bp") is None:
                out.append("floater_formula_partial")
            elif f.get("status") == "inferred":
                out.append("floater_formula_inferred")
            if f.get("benchmark") not in ("KEY_RATE", "RUONIA", "RUONIA_3M", "RUONIA_INDEX"):
                out.append("floater_benchmark_unsupported")
    if not t.is_perpetual and t.maturity_date is None:
        out.append("maturity_missing")
    if t.is_perpetual and not any(o.type == "call" and o.status == "scheduled" for o in t.offers):
        out.append("perpetual_no_call")
    if active and not t.is_perpetual and t.maturity_date and t.maturity_date > as_of:
        reds = [r for r in t.redemptions if r.date > as_of]
        if any(r.value_per_bond is None for r in reds):
            out.append("redemption_amount_missing")
        elif t.face_value is not None and abs(sum((r.value_per_bond for r in reds), Decimal(0)) - t.face_value) \
                > Decimal("0.01") and not t.is_face_indexed:
            out.append("redemption_mismatch")
    if t.face_value is None:
        out.append("face_missing")
    sub = (raw.get("bond_subtype") or "").lower()
    if active and "оферт" in sub and "call" not in sub and not row["offers_scheduled"]:
        out.append("offer_expected_none_scheduled")
    if any(o.date is None for o in t.offers):
        out.append("offer_date_missing")
    if t.face_unit and t.currency and t.face_unit != t.currency:
        out.append("currency_mismatch")
    if not t.isin:
        out.append("isin_missing")
    elif shared_isins.get(t.isin, 0) > 1:
        out.append("isin_shared")
    if row["issuer_name"] is None:
        out.append("issuer_missing")
    elif _DATED_FORM.search(row["issuer_name"]):
        out.append("issuer_name_dated")
    if row["short_name"] and not _CYRILLIC.search(row["short_name"]) and t.face_unit in (None, "RUB"):
        out.append("name_latin")
    if t.status == "matured":
        out.append("matured")
    return out


def _quotes(rows: list[dict[str, Any]], view: StaticView, market: Snapshot | None) -> None:
    if market is None:
        return
    mv = MarketView(market)
    from ..valuation.bonds import PRICE_BASIS_ORDER

    for r in rows:
        q = mv.quote(view.bonds[r["secid"]])
        r["has_quote"] = q is not None
        if q is not None:
            r["price_pct"] = next((q.prices_pct[b] for b in PRICE_BASIS_ORDER if b in q.prices_pct), None)
            r["num_trades"] = q.num_trades


def _core_market(rows: list[dict[str, Any]], snapshots: Path, market: Snapshot) -> None:
    """Full valuation of the active instruments (core, no fallback): its reason per bond."""
    from ..orchestrator import prepare, value

    ctx = prepare(SnapshotDir(Path(snapshots)), market.business_date, market_snapshot_id=market.id)
    act = [r for r in rows if r["status"] == "active"]
    for r, o in zip(act, value(ctx, [(r["secid"], 1.0) for r in act], fallback=False), strict=True):
        r["core_market"] = (o.valuation.quality if o.valuation else o.reason) or "not_valued"
        r["core_market_detail"] = ("; ".join(o.valuation.assumptions) if o.valuation else o.detail)[:300] or None


def build(static: Snapshot, market: Snapshot | None = None, *, active_only: bool = False,
          as_of: date | None = None) -> Export:
    view = StaticView(static)
    as_of = as_of or date.fromisoformat(static.business_date)
    raw = {i["secid"]: i for i in static.data["instruments"]}
    isins = Counter(i.get("isin") for i in static.data["instruments"] if i.get("isin"))
    for m in static.data.get("id_map", []):
        if m.get("isin") and m.get("isin_shared_with"):
            isins[m["isin"]] = max(isins[m["isin"]], 1 + len(m["isin_shared_with"]))
    issuers = {int(i["issuer_id"]): i for i in static.data["issuers"]}
    rows = [instrument_row(raw[s], t, view, as_of, isins, issuers) for s, t in sorted(view.bonds.items())
            if not active_only or t.status == "active"]
    _quotes(rows, view, market)
    return Export(static, market, rows)


# --------------------------------------------------------------------------------- summary

def _filled(v: Any) -> bool:
    return v not in (None, "", [], ())


def summary(rows: list[dict[str, Any]], fields: Iterable[str] = FILL_FIELDS) -> dict[str, Any]:
    """Counts for the audit: per bond class, the fill rate of each field, the reasons and the flags."""
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_class[r["bond_class"]].append(r)
    fields = list(fields)
    fill = {cls: {f: sum(_filled(r[f]) for r in rs) for f in fields} for cls, rs in by_class.items()}
    fill["_total"] = {f: sum(_filled(r[f]) for r in rows) for f in fields}
    return {
        "instruments": len(rows),
        "by_class": dict(Counter(r["bond_class"] for r in rows).most_common()),
        "by_status": dict(Counter(r["status"] for r in rows).most_common()),
        "by_coupon_type": dict(Counter(r["coupon_type"] for r in rows).most_common()),
        "fill": fill,
        "core_static": dict(Counter(r["core_static"] for r in rows if r["status"] == "active").most_common()),
        "core_static_by_class": {cls: dict(Counter(r["core_static"] for r in rs if r["status"] == "active")
                                           .most_common()) for cls, rs in by_class.items()},
        "core_market": dict(Counter(r["core_market"] for r in rows if r["core_market"]).most_common()),
        "flags": dict(Counter(f for r in rows for f in r["dq_flags"]).most_common()),
        "flags_by_class": {cls: dict(Counter(f for r in rs for f in r["dq_flags"]).most_common())
                           for cls, rs in by_class.items()},
    }


def summary_text(s: dict[str, Any]) -> str:
    n = s["instruments"]
    lines = [f"{n} instruments: " + ", ".join(f"{k} {v}" for k, v in s["by_status"].items()),
             "by class: " + ", ".join(f"{k} {v}" for k, v in s["by_class"].items()),
             "by coupon type: " + ", ".join(f"{k} {v}" for k, v in s["by_coupon_type"].items()),
             "filled (% of instruments): " + ", ".join(
                 f"{f} {100 * c / n:.0f}" for f, c in s["fill"]["_total"].items()) if n else "filled: -",
             "core, static checks (active): " + ", ".join(f"{k} {v}" for k, v in s["core_static"].items())]
    if s["core_market"]:
        lines.append("core, with the quote (active): " + ", ".join(f"{k} {v}" for k, v in s["core_market"].items()))
    lines.append("flags: " + ", ".join(f"{k} {v}" for k, v in s["flags"].items()))
    return "\n".join(lines)


# ---------------------------------------------------------------------------------- writers

def _cells(row: dict[str, Any]) -> list[str | None]:
    return [_s(row[k]) for k, _ in COLUMNS]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with Path(path).open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([k for k, _ in COLUMNS])
        for r in rows:
            w.writerow(["" if c is None else c for c in _cells(r)])


_NUM = re.compile(r"^-?\d+(\.\d+)?$")
_NUMERIC_COLUMNS = frozenset({
    "issuer_id", "coupon_frequency", "coupon_period_days", "implied_frequency", "coupons_total", "coupons_future",
    "coupons_future_no_value", "current_coupon_no", "current_coupon_rate_pct", "current_coupon_amount",
    "mon_coupon_pct", "coupon_benchmark_spread_pct", "floater_spread_bp", "floater_lag_days", "face_value",
    "initial_face_value", "amortizations_future", "redemptions_future_sum", "offers_scheduled", "listing_level",
    "issue_size", "price_pct", "num_trades"})


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet(table: list[list[Any]], numeric: set[int] = frozenset(), freeze: bool = True) -> str:  # type: ignore[assignment]
    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
    if freeze:
        out.write('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft"'
                  ' state="frozen"/></sheetView></sheetViews>')
    out.write("<sheetData>")
    for ri, row in enumerate(table, start=1):
        out.write(f'<row r="{ri}">')
        for ci, v in enumerate(row):
            if v is None or v == "":
                continue
            ref = f"{_col(ci)}{ri}"
            text = str(v)
            if ri > 1 and ci in numeric and _NUM.match(text):
                out.write(f'<c r="{ref}"><v>{text}</v></c>')
            else:
                style = ' s="1"' if ri == 1 else ""
                out.write(f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{escape(text)}</t></is></c>')
        out.write("</row>")
    out.write("</sheetData>")
    if freeze and table:
        out.write(f'<autoFilter ref="A1:{_col(len(table[0]) - 1)}{len(table)}"/>')
    out.write("</worksheet>")
    return out.getvalue()


def write_xlsx(path: Path, rows: list[dict[str, Any]], summ: dict[str, Any] | None = None,
               meta: list[tuple[str, str]] = ()) -> None:  # type: ignore[assignment]
    keys = [k for k, _ in COLUMNS]
    sheets = [("instruments", _sheet([keys] + [_cells(r) for r in rows],
                                     {i for i, k in enumerate(keys) if k in _NUMERIC_COLUMNS}))]
    s = summ or summary(rows)
    table: list[list[Any]] = [["section", "key", "value"]]
    table += [["meta", k, v] for k, v in meta]
    for sec in ("by_status", "by_class", "by_coupon_type", "core_static", "core_market", "flags"):
        table += [[sec, k, v] for k, v in s[sec].items()]
    n = s["instruments"] or 1
    table += [["filled_pct", f, round(100 * c / n, 1)] for f, c in s["fill"]["_total"].items()]
    sheets.append(("summary", _sheet(table, {2})))
    sheets.append(("columns", _sheet([["column", "meaning"], *[[k, v] for k, v in COLUMNS],
                                      *[[f"flag:{k}", v] for k, v in FLAGS.items()]])))
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" '
          'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" '
          'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
          + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    for i in range(1, len(sheets) + 1)) + "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/></Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          + "".join(f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>' for i, (n, _) in enumerate(sheets, start=1))
          + "</sheets></workbook>")
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                         f'relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                         for i in range(1, len(sheets) + 1))
               + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/'
               '2006/relationships/styles" Target="styles.xml"/></Relationships>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
              '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill>'
              '<fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
              '</styleSheet>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", styles)
        for i, (_, xml) in enumerate(sheets, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", xml)


def export(out: Path, snapshots: Path, *, on: str | None = None, static_file: Path | None = None,
           market_file: Path | None = None, active_only: bool = False, with_value: bool = False
           ) -> tuple[Export, dict[str, Any]]:
    static, market = find_snapshots(snapshots, on, static_file, market_file)
    ex = build(static, market, active_only=active_only)
    if with_value:
        if market is None:
            raise SnapshotError(f"--value needs an eod_final market snapshot of {static.business_date}")
        _core_market(ex.rows, snapshots, market)
    summ = summary(ex.rows)
    out = Path(out)
    if out.suffix.lower() == ".xlsx":
        meta = [("static_snapshot_id", static.id), ("business_date", static.business_date),
                ("known_at", static.doc.get("known_at") or ""),
                ("market_snapshot_id", market.id if market else "")]
        write_xlsx(out, ex.rows, summ, meta)
    else:
        write_csv(out, ex.rows)
    return ex, summ
