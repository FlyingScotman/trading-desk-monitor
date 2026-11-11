"""«Индексы ставок» (TDM#565, TDM#651): why the screen has no table from the adapter, and the table the monitor can
build itself.

The screen's table normally comes from the adapter's action (``choices`` kind ``trade_index_codes``, 08 §3). That
answer may be missing for many reasons: no adapter, an adapter that does not load or has no such action, a run that
failed or timed out, an answer the monitor rejected, an answer with ``ok`` false (the adapter's own reason), an answer
with no floating legs. ``state`` names which one it is, in the owner's words and with the button to press, so the
screen never shows a bare «индексы не получены».

The names themselves are already in the store: the adapter keeps the report's name of a floating leg's index in
``x_index`` of every ``trade_terms`` row (08 §10), and the store keeps the ``x_*`` fields beside the canonical ones.
``stored_choices`` builds the screen's ``choices`` from them (the rows in force, one row per name) without calling the
adapter at all: the dropdown holds the codes of the adapter's own settings tab, a save writes into that tab as for the
adapter's table. Names are the terms' own values: they go to the local page only, never to a log or an issue.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

from . import adapter_jobs, store
from .inbox import loader

KIND = "trade_index_codes"
TAB_ID = "index_codes"
"""The settings tab and its object the kind saves into (08 §3: ``{"tab": "index_codes", "key": "index_codes"}``)."""
TAB_KEY = "index_codes"
MAX_NAMES = 500
NONE_TITLE = "не выбрано — как в настройках источника, иначе «другой индекс»"
COLUMNS = (("source", "Источник"), ("legs", "Ног"), ("trades", "Сделок"), ("books", "Книг"),
           ("ccys", "Валюты ног"), ("kinds", "Вид"), ("last_file", "Последний файл"))
SCREEN = {"trade_index_codes": "Индексы ставок", "ocp_derivative_types": "ОВП: типы строк отчёта"}
WHERE_ADAPTER = "Настройки → «Адаптер и источники»"
WHERE_UPDATES = "Настройки → «Обновления»"
ERROR_TEXTS = {
    "unsupported_spi": "адаптер написан для более новой версии интерфейса, чем знает этот монитор: обновите монитор "
                       f"({WHERE_UPDATES})",
    "unsupported_contract": "адаптер написан для более нового контракта, чем знает этот монитор: обновите монитор "
                            f"({WHERE_UPDATES})",
    "spi_mismatch": f"адаптер объявляет разные версии интерфейса (ошибка его сборки): обновите адаптер ({WHERE_UPDATES})",
    "fingerprint_mismatch": "файлы адаптера не совпадают с закреплённой контрольной суммой: адаптер заменили или "
                            f"испортили; обновите его ({WHERE_UPDATES}) или обновите контрольную сумму ({WHERE_ADAPTER})",
    "config_dir_missing": f"папка настроек адаптера не найдена: проверьте её путь ({WHERE_ADAPTER})",
    "not_found": f"файлы адаптера не найдены: проверьте путь к адаптеру ({WHERE_ADAPTER})",
    "import_error": f"код адаптера не загружается (ошибка в нём или нет нужной библиотеки): запустите «Проверку "
                    f"адаптера» ({WHERE_ADAPTER})",
    "name_collision": f"имя пакета адаптера совпадает с модулем программы: обновите адаптер ({WHERE_UPDATES})",
    "missing_function": f"в адаптере нет обязательных функций: обновите адаптер ({WHERE_UPDATES})",
    "describe_failed": f"адаптер упал при описании себя: запустите «Проверку адаптера» ({WHERE_ADAPTER})",
    "bad_describe": f"адаптер описал себя неверно (вероятно, его версия не подходит к монитору): обновите адаптер "
                    f"({WHERE_UPDATES})",
    "bad_source": f"адаптер описал источник данных, которого монитор не знает: обновите монитор ({WHERE_UPDATES})",
    "missing_reference": f"адаптер описан неполно (вероятно, его версия не подходит к монитору): обновите адаптер "
                         f"({WHERE_UPDATES})",
}
JOB_TEXTS = {
    "timeout": "адаптер не успел прочитать условия сделок за отведённое время. Нажмите «Обновить»; если снова не "
               "успеет — увеличьте время одного действия адаптера (Настройки → «Адаптер и источники» → «Одно "
               "действие адаптера») или уменьшите «Сколько последних файлов» в его действии",
    "interrupted": "сервер останавливался во время чтения условий сделок. Нажмите «Обновить»",
    "bad_result": "ответ адаптера не прошёл проверку монитора (вероятно, версии адаптера и монитора не совпадают): "
                  f"обновите адаптер ({WHERE_UPDATES}); если не помогло — запустите «Проверку адаптера» "
                  f"({WHERE_ADAPTER})",
    "failed": "адаптер упал при чтении условий сделок. Нажмите «Обновить»; если повторится — запустите «Проверку "
              f"адаптера» ({WHERE_ADAPTER}) и обновите адаптер ({WHERE_UPDATES})",
    "write": "результат действия не удалось записать на диск: проверьте место на диске и права на папку "
             f"результатов действий ({WHERE_ADAPTER} → «Действия адаптера»), затем нажмите «Обновить»",
    "internal": "монитор не смог выполнить действие (внутренняя ошибка). Нажмите «Обновить»; если повторится — "
                "сохраните диагностику (Настройки → «Диагностика») и передайте разработчику",
}


def _st(code: str, level: str, text: str, **more: Any) -> dict[str, Any]:
    return {"code": code, "level": level, "text": text, **more}


def adapter_state(h: Any, kind: str) -> dict[str, Any] | None:
    """Why the adapter cannot give the screen at all (it is not loaded, or has no action of the kind), else None."""
    screen = SCREEN.get(kind, kind)
    if not h.ok:
        code = str(h.error or "").split(":", 1)[0].strip() or "load_failed"
        why = ERROR_TEXTS.get(code)
        if why is None and code.startswith(("describe_", "locate_")):
            why = (f"адаптер не ответил при загрузке ({code}): нажмите «Проверка адаптера» ({WHERE_ADAPTER}); если "
                   f"адаптер новый — обновите его ({WHERE_UPDATES})")
        if why is None:
            why = f"адаптер не загрузился ({code}): запустите «Проверку адаптера» ({WHERE_ADAPTER})"
        return _st("adapter_error", "bad", f"Экран «{screen}»: {why}.", error_code=code)
    if not h.spec:
        return _st("adapter_off", "warn", f"Экран «{screen}»: приватный адаптер не подключён, монитор не знает названий "
                   f"индексов из условий сделок. Подключите адаптер ({WHERE_ADAPTER}).")
    return _st("no_action", "warn", f"Экран «{screen}»: адаптер {h.name} {h.version} не умеет отдавать этот список (у "
               f"него нет нужного действия). Обновите адаптер ({WHERE_UPDATES}).", adapter=f"{h.name} {h.version}")


def state(job: dict | None, running: bool, choices: dict | None, source: str | None, kind: str = KIND) -> dict[str, Any]:
    """The screen's state, for an adapter that is loaded and has the action: ``{code, level, text, ...}``. ``level``
    is ``ok`` / ``info`` / ``warn`` / ``bad``; ``source`` is where ``choices`` come from (``adapter`` | ``store`` |
    None). Texts hold counts, codes and the adapter's own ``summary`` lines (which hold no name), never a name."""
    screen = SCREEN.get(kind, kind)
    res = (job or {}).get("result") or {}
    if running:
        return _st("running", "info", "Адаптер читает последние файлы условий сделок…")
    if job is None:
        return _st("never_run", "info", f"Экран «{screen}» ещё не запрашивал индексы у адаптера: нажмите «Обновить».")
    j_state = job.get("state")
    if j_state in ("failed", "timeout"):
        code = job.get("error_code") or ("timeout" if j_state == "timeout" else "failed")
        text = JOB_TEXTS.get(code) or JOB_TEXTS["failed"]
        return _st("job_" + code, "bad", f"Адаптер не дал индексы: {text}.", error_code=code)
    if res.get("ok") is False:
        lines = [str(x) for x in res.get("summary") or [] if str(x).strip()]
        return _st("adapter_declined", "warn", "Адаптер ответил, что индексов нет: " + ("; ".join(lines) if lines else
                   "причину не назвал (нажмите «Проверка адаптера», " + WHERE_ADAPTER + ")") + ".", summary=lines)
    if res.get("choices") is None and res.get("warnings"):  # the monitor left a broken table out (08 §3)
        return _st("adapter_bad_choices", "bad", "Адаптер прислал список индексов в неверной форме, монитор его не принял "
                   "(вероятно, версии адаптера и монитора не совпадают): обновите адаптер (" + WHERE_UPDATES + "); если "
                   "не помогло — запустите «Проверку адаптера» (" + WHERE_ADAPTER + ").")
    if not (res.get("choices") or {}).get("rows"):
        base = ("Адаптер просмотрел файлы условий сделок, но плавающих ног с названием индекса в них не нашёл"
                if res.get("choices") else "Адаптер ответил без списка индексов")
        tail = (" Если плавающие сделки в мониторе есть, их условия приходят не файлами из «Папок с отчётами» "
                "(например, из сервиса сделок): список ниже взят из уже загруженных условий." if source == "store" else
                " Если плавающие сделки в мониторе есть, их условия приходят не файлами из «Папок с отчётами»: "
                "список появится, когда условия загружены в монитор.")
        return _st("adapter_empty", "warn", base + "." + tail)
    return _st("ok", "ok", "")


# ---- the names the store already holds -------------------------------------------------------------------------------

_SQL = f"""
WITH cur AS (SELECT DISTINCT ON (scope_key) set_id, source, report_date, valid_date FROM dataset_set
             WHERE dataset = 'trade_terms' AND valid_date <= current_date AND recorded_at <= now()
             ORDER BY scope_key, {store.CURRENT_SET})
SELECT lower(btrim(r.x->>'x_index')) AS k,
       mode() WITHIN GROUP (ORDER BY btrim(r.x->>'x_index')) AS label,
       count(*) AS legs, count(DISTINCT r.data->>'trade_id') AS trades, count(DISTINCT r.data->>'book_id') AS books,
       array_remove(array_agg(DISTINCT r.data->>'ccy'), NULL) AS ccys,
       array_remove(array_agg(DISTINCT r.data->>'product_kind'), NULL) AS kinds,
       count(*) FILTER (WHERE r.data->>'maturity_date' >= current_date::text) AS live,
       mode() WITHIN GROUP (ORDER BY r.data->>'index_code') AS code,
       bool_or(r.x->>'x_index_unmapped' IN ('true', 'True', '1')) AS unmapped,
       max(cur.report_date) AS last_day,
       (array_agg(cur.source ORDER BY cur.report_date DESC NULLS LAST, cur.set_id DESC))[1] AS source,
       array_agg(DISTINCT cur.source) AS sources
FROM cur JOIN dataset_row r ON r.set_id = cur.set_id
WHERE btrim(coalesce(r.x->>'x_index', '')) <> ''
GROUP BY 1 ORDER BY count(*) DESC, 2 LIMIT {MAX_NAMES}"""


def stored_names(store_url: str | None) -> list[dict[str, Any]] | None:
    """One dict per index name of the floating legs of the ``trade_terms`` rows in force today (the current set of each
    unit, as ``store.dataset_rows`` reads), most legs first; ``None`` when there is no store or it cannot be read."""
    if not store_url:
        return None
    try:
        with psycopg.connect(store_url, autocommit=True, connect_timeout=3) as con:
            rows = con.execute(_SQL).fetchall()
    except Exception:  # noqa: BLE001 - no store, an older schema: no list, never an error of the screen
        return None
    keys = ("key", "label", "legs", "trades", "books", "ccys", "kinds", "live", "code", "unmapped", "last_day",
            "source", "sources")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def _tab(h: Any) -> dict | None:
    return next((t for t in loader.check_settings_tabs(h.describe)[0] if t["id"] == TAB_ID), None)


def _tab_options(tab: dict) -> list[dict[str, Any]]:
    """The codes the tab's object takes (the schema's enum under ``additionalProperties``, with the titles of its
    ``oneOf``), as the dropdown's options; [] when the schema says nothing the monitor can read."""
    prop = ((tab.get("schema") or {}).get("properties") or {}).get(TAB_KEY)
    item = prop.get("additionalProperties") if isinstance(prop, dict) else None
    if not isinstance(item, dict) or not isinstance(item.get("enum"), list):
        return []
    titles = {o.get("const"): o.get("title") for o in item.get("oneOf") or () if isinstance(o, dict)}
    return [{"value": c, "title": str(titles.get(c) or c)} for c in item["enum"] if isinstance(c, str)]


def _file_map(path: Path | None) -> dict[str, str]:
    """``{name as the file spells it: code}`` of the tab's object; {} for a file that is missing or not readable."""
    if path is None:
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig")).get(TAB_KEY)
    except (OSError, ValueError, AttributeError):
        return {}
    return {str(k): v for k, v in obj.items() if isinstance(v, str)} if isinstance(obj, dict) else {}


def _key(name: str) -> str:
    return name.strip().casefold()


def stored_choices(st: Any, h: Any, store_url: str | None, replay_sources: set[str] | None = None) -> dict | None:
    """The screen's ``choices`` built from the store's ``trade_terms`` names and the adapter's tab (module doc), or
    ``None``: no store, no name stored yet, the adapter does not declare the tab. Same shape as the adapter's, plus
    ``source`` ``"store"`` and ``read_only`` (the tab or its codes are not readable: the list is shown, a save is not
    offered). ``replay_sources``: the adapter's sources, to name the one to read again after a save."""
    names = stored_names(store_url)
    if not names:
        return None
    tab = _tab(h)
    path = adapter_jobs.choices_file(st, h, {"save": {"tab": TAB_ID}})[1] if tab else None
    options = _tab_options(tab) if tab else []
    editable = bool(tab and path and options)
    fmap = _file_map(path)
    by_key: dict[str, list[tuple[str, str]]] = {}
    for k, v in fmap.items():
        by_key.setdefault(_key(k), []).append((k, v))
    codes = {o["value"] for o in options}
    opts = [{"value": None, "title": NONE_TITLE}, *options]
    rows = []
    for n in names:
        label = n["label"]
        own = by_key.get(_key(label), [])
        value, hint = None, None
        if own and own[-1][1] in codes:
            value = own[-1][1]
        elif n["code"] and not n["unmapped"] and n["code"] != "OTHER" and n["code"] in codes:
            value = n["code"]
            hint = f"в загруженных условиях сделок индекс этого названия — {n['code']} (задан не на этой вкладке)"
        if value is None and (n["unmapped"] or n["code"] == "OTHER"):
            hint = "в загруженных условиях сделок это название без сопоставления: нога идёт как «другой индекс»"
        rows.append({"key": label, "label": label, "value": value, "suggested": None, "hint": hint, "new": False,
                     "future": int(n["live"] or 0), "replaces": sorted(k for k, _v in own if k != label),
                     "children": [], "children_more": 0,
                     "cells": {"source": ", ".join(sorted(s for s in n["sources"] if s)), "legs": n["legs"],
                               "trades": n["trades"], "books": n["books"], "ccys": ", ".join(n["ccys"]),
                               "kinds": ", ".join(n["kinds"]),
                               "last_file": n["last_day"].isoformat() if isinstance(n["last_day"], date) else None}})
    pick = next((n for n in names if n["unmapped"]), names[0])
    replay = None
    if (pick["source"] and isinstance(pick["last_day"], date)
            and (replay_sources is None or pick["source"] in replay_sources)):
        replay = {"source": pick["source"], "from": pick["last_day"].isoformat(), "to": pick["last_day"].isoformat()}
    notes = [("Список взят из условий сделок, уже загруженных в монитор, а не от адаптера: он показывает названия "
              "индексов плавающих ног и то, как монитор сопоставил их сейчас.")]
    if not editable:
        notes.append("Вкладки «Индексы ставок» у адаптера нет или она не называет коды: выбор отсюда записать нельзя, "
                     f"обновите адаптер ({WHERE_UPDATES}).")
    elif replay:
        notes.append(f"Выбор записывается на вкладку адаптера «Индексы ставок» ({WHERE_ADAPTER}); затем перечитываются "
                     f"условия за {replay['to']}.")
    else:
        notes.append(f"Выбор записывается на вкладку адаптера «Индексы ставок» ({WHERE_ADAPTER}). Чтобы условия "
                     "обновились, нажмите «Перечитать отчёты за период» (там же).")
    return {"kind": KIND, "title": "Индексы ставок в загруженных условиях сделок",
            "save": {"tab": TAB_ID, "key": TAB_KEY}, "options": opts,
            "columns": [{"id": c, "title": t} for c, t in COLUMNS], "rows": rows, "risky_unset": [],
            "replay": replay, "source": "store", "read_only": not editable, "truncated": len(names) >= MAX_NAMES,
            "warnings": [], "notes": notes}
