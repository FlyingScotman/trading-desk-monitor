# Центральный сервис данных: что хранит и что отдаёт

`tdm` — центральный источник данных для четырёх мониторов (Desk Monitor, Widget Board, Pivot Studio, Scenario Lab) и для любых других потребителей: Excel, Python, BI. Он хранит позиции, инструменты, рыночные данные и результаты расчётов и отдаёт их по HTTP. Сам сервис ничего не переоценивает.

Связанные документы:

| Документ | О чём |
|---|---|
| `docs/DATA_REQUIREMENTS.md` | **список данных к сбору**: каждый набор, поле, источник, шаблон CSV |
| `docs/DATA_CONTRACT.md` | результаты расчётов: прогоны, риски, PnL explain, как их пишет расчётный сервис |
| `docs/SCENARIOS.md` | логика сценариев: что считает монитор, что — расчётный сервис, заранее или по запросу |
| `tdm/schema.sql` | DDL всех таблиц |

---

## 1. Архитектура

```
   ВАШИ ИСТОЧНИКИ                         tdm  (SQLite, один файл)                                ПОТРЕБИТЕЛИ
 ┌──────────────────────┐   CSV    ┌────────────────────────────────────────────────┐   HTTP   ┌─────────────────────┐
 │ снэпшоты позиций и   ├─────────►│ 1. ВХОДНЫЕ ДАННЫЕ (по датам)                    ├─────────►│ 4 монитора           │
 │ фондирования (EOD)   │ tdm load │   справочники  ref_*                            │ /api/v1  │ (браузер)            │
 │ справочники бумаг,   │          │   срезы        pos_snapshot, fund_snapshot,     │          ├─────────────────────┤
 │ сделок, эмитентов    │          │                trade_log, pnl_official          │          │ Excel / Python / BI  │
 │ рынок: кривые, курсы,│          │   рынок        md_curve_point, md_fx, md_fixing,│          └─────────────────────┘
 │ фиксинги, котировки  │          │                md_bond_quote, md_cds_quote      │
 │ официальный PnL      │          ├────────────────────────────────────────────────┤
 └──────────────────────┘          │ 2. РЕЗУЛЬТАТЫ РАСЧЁТОВ (по прогонам)            │◄── пишет ─┐
                                   │   calc_run, position(+risk, fx, pnl),           │           │
 ┌──────────────────────┐  читает  │   funding_deal(+pnl), scenario_result           │           │
 │ РАСЧЁТНЫЙ СЕРВИС     │◄─────────┤ 3. ПРОИЗВОДНЫЕ (строит tdm)                     │           │
 │ in-house либа        ├──────────┼─► пилларсы кривых, изменения за день, спреды по │───────────┘
 └──────────────────────┘ writer / │   рейтингам, исторические сценарии,             │
                          import   │   desk_history, покрытие данных                 │
                                   └────────────────────────────────────────────────┘
```

**Три слоя:**

1. **Входные данные** привязаны к **дате**. Это то, что вы добываете (`docs/DATA_REQUIREMENTS.md`). Грузятся CSV-файлами: `python -m tdm load`. Повторная загрузка за ту же дату заменяет срез; справочники обновляются по ключу.
2. **Результаты расчётов** привязаны к **прогону**: риски, PnL explain, полная переоценка. Пишет расчётный сервис (`tdm.writer.Run` или JSON-пакет). Прогон виден мониторам только после успешной публикации, поэтому недосчитанное или упавшее никто не увидит.
3. **Производные** `tdm` вычисляет сам из первых двух слоёв: простая арифметика без моделей оценки.

**Почему расчёты отделены от данных.** Мониторам нужны и сырые данные (позиции, рынок, история), и результаты либы (key-rate DV01, PnL explain). Либа живёт в вашем продукте. Сервис хранит и то и другое и гарантирует, что монитор показывает результат, посчитанный именно на тех входах, которые лежат в базе. Для этого расчётный сервис берёт статику и рынок из `tdm`: `run.add_reference_data()` или API.

---

> **Контракт 1.3** (`docs/DATA_CONTRACT.md` §0, `docs/ORG_CONVENTIONS.md`): режимы книг и юрлица, строки фондирования по позициям и остаткам ДС,
> справочник ставок фондирования, курсы ЦБ рядом с рыночными, официальный PnL на T+1, график заседаний ЦБ и сценарии кэрри.
> Новые сущности помечены *1.3*. Бэкенд их реализует (схема v5, `docs/DATA_CONTRACT.md` §0.9); страницы получают ту же форму, что в прототипах.

## 2. Сущности и ключи

```
ref_book ─┬─< pos_snapshot >─── ref_instrument ──< ref_cashflow
          │        │                   │
          │        └──── ref_otc_deal ─┼── ref_counterparty
          ├─< fund_snapshot            │
          ├─< trade_log                ref_issuer ──< ref_issuer_rating
          └─< pnl_official                 │
                                           └──< md_bond_quote (через инструмент), md_cds_quote
ref_curve ──< md_curve_point     md_fx     md_fixing     ref_calendar
ref_funding_rate (1.3)   cash_snapshot (1.3, ref_book × валюта)   md_fx_cbr (1.3)   ref_cbr_meeting (1.3)   carry_scenario_def (1.3)

calc_run ──< position ──< position_risk / position_fx / position_gamma / position_pnl / scenario_result
         ──< funding_deal ──< funding_pnl                     (строки POS / HTM ссылаются на position)
         ──< funding_rule, run_ocp_transfer                    (1.3: снимок справочника ставок, сводка передачи ОВП)
         ──< run_book / run_issuer / run_curve / mkt_*        (снимок статики и рынка, на котором считали)
scenario_def ──< scenario_result        desk_history        data_load        scenario_job
```

| Ключ | Что это | Требование |
|---|---|---|
| `book_id` | книга | стабилен; один на все наборы |
| `instrument_id` | бумага | лучше ISIN; не переиспользуется |
| `deal_id` | OTC-сделка или сделка фондирования | номер из фронт- или бэк-системы |
| `position_id` | позиция в срезе | **стабилен между днями**: бумага — `book_id\|ISIN`, OTC — `deal_id`. По нему определяются новые позиции, изменения за день и сопоставление с результатами расчётов |
| `issuer_id` | эмитент или референс | ИНН / LEI / внутренний код. Монитор показывает `name` |
| `curve_id` | кривая | `RUB OFZ`, `RUB RUONIA`, `RUB KEY`, `USD SOFR`, `CNY`, `EUR ESTR`, `CHF SARON` (UI ссылается на них по имени) |
| `entity` | *1.3* юрлицо | атрибут книги; иерархия юрлицо → деск → книга |
| `book_type` | *1.3* режим фондирования книги | `TRADING` / `HTM` / `DERIVS`, ровно один на книгу |
| `as_of` / `date` | рабочий день рынка | `YYYY-MM-DD` |

**Сквозное правило.** `position_id` в срезе позиций (`pos_snapshot`) и в результатах расчёта (`position`) — **один и тот же**. Тогда монитор может показать рядом сырые атрибуты позиции и её риски.

---

## 3. Что хранится

### 3.1 Входные данные (слой 1)

Полный перечень полей — `docs/DATA_REQUIREMENTS.md` §4. Краткая сводка:

| Группа | Таблицы | Гранулярность | Глубина |
|---|---|---|---|
| Справочники | `ref_book` (*1.3*: `book_type`, `entity`, `ocp_to`), `ref_issuer`, `ref_issuer_rating`, `ref_counterparty`, `ref_instrument`, `ref_cashflow`, `ref_otc_deal`, `ref_curve`, `ref_calendar`, *1.3*: `ref_funding_rate`, `ref_cbr_meeting` | актуальная версия; рейтинги — история по датам | всё, что встречалось за год |
| Срезы | `pos_snapshot` (*1.3*: поля HTM), `fund_snapshot` (*1.3*: только РЕПО и особые сделки), *1.3*: `cash_snapshot`, `trade_log`, `pnl_official` (*1.3*: компоненты, базис ЦБ) | рабочий день × позиция / сделка / книга × валюта | 1 год |
| Рынок | `md_curve_point`, `md_fx` (рыночный), *1.3*: `md_fx_cbr`, `md_fixing` (+ `RUSFAR O/N`, `SARON`), `md_bond_quote`, `md_cds_quote` | рабочий день × узел / валюта / индекс / бумага | 1 год |
| Журнал | `data_load` | файл | всё |

### 3.2 Результаты расчётов (слой 2)

Полностью описаны в `docs/DATA_CONTRACT.md`. По каждому прогону (EOD или интрадей):

| Что | Таблицы | Используется в мониторе |
|---|---|---|
| Позиция с оценкой | `position`: MV, доходность, спред, дюрации, DV01, CS01, JTD | все экраны |
| Key-rate DV01 | `position_risk`: кривая × 11 бакетов | теплокарты, лестницы, сценарии, explain по ставкам |
| FX-дельта | `position_fx` | ОВП, FX-сценарии |
| Вторые производные: гамма, выпуклость, кросс-гамма | `position_gamma` — разреженная матрица по факторам `IR\|кривая\|бакет`, `CS`, `FX\|валюта` | второй порядок в сценариях («Выпуклость», «Кросс FX»), ранжирование исторических окон, колонки гамм |
| PnL explain | `position_pnl`: carry, rolldown, rates, spread, fx, *1.3* funding, convexity, new trades; FX по курсам ЦБ; official (T+1) | вкладка PnL, Scenario Lab → explain |
| Фондирование | `funding_deal` (*1.3*: строки `POS` / `HTM` / `CASH` / `REPO`), `funding_pnl`, *1.3* `funding_rule` (снимок справочника ставок) | вкладка «Фондирование», ОВП, сценарии кэрри |
| Полная переоценка сценариев | `scenario_result`, `scenario_funding_result` | вкладка «Сценарии», Scenario Lab |
| Снимок статики и рынка прогона | `run_book`, `run_issuer`, `run_curve`, `mkt_curve_point`, `mkt_fx`, `mkt_index` | чтобы прошлый прогон выглядел так, как был посчитан |

### 3.3 Производные (слой 3, строит `tdm`)

| Что | Как | Где видно |
|---|---|---|
| Кривые на 11 пилларах монитора | линейная интерполяция узлов `md_curve_point` по сроку, плоская на концах | `/market/curves`, `run.add_reference_data()` |
| Изменение кривых за день, б.п. | пилларсы(d) − пилларсы(предыдущая дата с данными) | `/market/curves`, `CURVE_MOVES` в прогоне |
| Изменение курсов, % | курс(d) / курс(prev) − 1 | `/market/fx`, `FX_MOVE_PCT` |
| Последний фиксинг индекса | последнее значение не позже даты | `/market/fixings`, `INDICES` |
| Рейтинг на дату | последняя запись `issuer_ratings` не позже даты (COMPOSITE приоритетнее) | `/issuers?as_of=`, прогоны |
| Спред эмитента и его изменение | среднее Z-спреда (иначе G-спреда) бумаг эмитента | `/market/issuer-spreads` |
| Изменение спредов по рейтингам | медиана изменений по эмитентам корзины, без суверена | `/market/issuer-spreads`, исторические сценарии |
| Исторические сценарии | `build-hist-scenarios`, `docs/SCENARIOS.md` §4 | `/scenarios`, все экраны со сценариями |
| *1.3* Курсы ЦБ на дату | курс, действующий на дату (последняя запись `md_fx_cbr` не позже); курс на завтра — первая запись после даты; изменение — к прошлому рабочему дню | `/market/fx-cbr`, `FX_CBR*` в прогоне (`add_reference_data`) |
| *1.3* Фондирование облигаций торговых книг | если расчётный сервис его не выложил — `run.derive_funding()`: −чистая MV × ставка по справочнику (снимок `ref_funding_rate` в прогоне; КС / RUSFAR CNY по каждому календарному дню с прошлого рабочего дня; процент в RUB по курсу ЦБ) | строки `POS` прогона, `position_pnl.funding` и `fx_funding` |
| *1.3* Фондирование HTM | −цена покупки × номинал × контрактная ставка | строки `HTM` |
| *1.3* Фондирование книг деривативов | остатки `cash_snapshot` × O/N валюты по дням (RUB, CNY — 365; USD, EUR, CHF — 360) | строки `CASH` прогона |
| *1.3* Передача ОВП | `run.add_ocp_transfers()`: по сделкам передачи в прогоне (книги `TRADING` → `ocp_to`), курс ЦБ на завтра; при публикации — остаток ОВП и сверка с `otc_deals.purpose = OCP_TRANSFER` | `OCP_TRANSFERS`, ОВП по книгам |
| *1.3* Официальный PnL на T+1 | загрузка `pnl_official` за T, затем `python -m tdm attach-official --as-of T` (`tdm.writer.attach_official_pnl`): официальный PnL прикрепляется к опубликованному EOD-прогону за T, строка `desk_history` пересчитывается | вкладка PnL, сверка |
| Новые позиции дня | нет в предыдущем срезе | `/positions` → `new_today` |
| История итогов деска | автоматически из EOD-прогонов, либо загружается (`desk_history.csv`) | графики PnL и спарклайны DV01 / CS01 |
| Покрытие данных | сколько строк в каждом наборе на каждую дату, есть ли EOD-прогон | `/coverage`, `python -m tdm coverage` |

---

## 4. Что сервис отдаёт: API (только чтение)

Базовый адрес — `http://<сервер>:8080/api/v1/`. Ответ — JSON в UTF-8. Даты в параметрах — `YYYY-MM-DD`; неверная дата даёт ответ 400 с текстом ошибки. Если `as_of` не указан, берётся последняя доступная дата. Ряды фильтруются параметрами `from` и `to` (включительно).

### 4.1 Справочники

| Запрос | Ответ |
|---|---|
| `GET /books` | `{books:[{book_id,name,desk,trader,active,ord}]}` |
| `GET /issuers?as_of=` | `{issuers:[{issuer_id,name,issuer_group,sector,country,is_gov,is_quasi,rating}]}` — рейтинг на дату |
| `GET /issuers/ratings?issuer_id=` | история рейтингов |
| `GET /counterparties` | контрагенты |
| `GET /curves` | определения кривых |
| `GET /instruments?as_of=&issuer_id=&q=` | бумаги; `as_of` — непогашенные на дату; `q` — поиск по имени или ISIN |
| `GET /instruments/<id>` | `{instrument, cashflows:[…], last_quote}` |
| `GET /otc-deals?as_of=&book=` | условия OTC-сделок; `as_of` — живые на дату |
| `GET /funding-rules` | *1.3* справочник ставок фондирования: `{rules:[{book_type,desk,book_id,entity,instrument_id,ccy,index_name,spread_bp,fixed_rate,note}]}`; пустое поле — `null` («любое») |
| `GET /cbr-meetings?from=` | *1.3* заседания ЦБ: `{meetings:[{date, effective}]}`; `effective` по умолчанию — следующий рабочий день |
| `GET /carry-scenarios` | *1.3* сценарии кэрри и фондирования: `{scenarios:[{scenario_id,name,ord,active,definition:{ks,on,fundSpread,hardCcy}}]}` |

### 4.2 Срезы

| Запрос | Ответ |
|---|---|
| `GET /positions?as_of=&book=` | `{as_of, prev_as_of, positions:[{…pos_snapshot, name, ccy, instr_type, isin, issuer_id, asset_class, segment, maturity, side, counterparty_id, new_today}]}` |
| `GET /funding?as_of=&book=` | срез фондирования (с 1.3 — РЕПО и особые сделки) |
| `GET /cash-balances?as_of=&book=` | *1.3* остатки ДС книг деривативов: `{as_of, balances:[{as_of,book_id,ccy,balance}]}` |
| `GET /trades?date=` | сделки дня |
| `GET /pnl/official?as_of=` | официальный PnL по позициям и книгам за день |
| `GET /pnl/official?from=&to=` | ряд по книгам: строка книги, а если её нет — сумма позиций |

### 4.3 Рынок

| Запрос | Ответ |
|---|---|
| `GET /market/curves?as_of=` | `{as_of, prev, pillars, curves:{id:{rates[11], move_bp[11]}}, raw_points:[…]}` |
| `GET /market/curves?curve=RUB%20OFZ&from=&to=` | ряд: `{buckets, series:[{date, rates[11]}]}` |
| `GET /market/fx?as_of=` | `{fx:{ccy:{rate, move_pct}}}` |
| `GET /market/fx?ccy=USD&from=&to=` | ряд |
| `GET /market/fx-cbr?as_of=` | *1.3* курсы ЦБ: `{as_of, prev, fx:{ccy:{rate, move_pct, next}}}` — действующий на дату, изменение к прошлому рабочему дню, курс на завтра |
| `GET /market/fx-cbr?ccy=USD&from=&to=` | *1.3* ряд по дате действия: `{series:[{date, rate, set_date}]}` |
| `GET /market/fixings?as_of=` | последние фиксинги на дату |
| `GET /market/fixings?index=RUONIA&from=&to=` | ряд |
| `GET /market/bonds?as_of=` | котировки бумаг за день |
| `GET /market/bonds?instrument_id=&from=&to=` | ряд котировок бумаги как есть |
| `GET /bonds/history?instrument_id=\|isin=&curve=&from=&to=&wal=` | **история выпуска для мониторов**: котировки + ставка кривой `curve` на сроке выпуска в каждую дату (до оферты, пока она впереди, иначе до погашения; ИЦБ — `wal` + прошедшее время) + спред к ней + номинал позиции по снэпшотам. Формат — `docs/DATA_CONTRACT.md` §6 |
| `GET /market/issuer-spreads?as_of=` | `{issuers:{id:{spread_bp, move_bp}}, rating_moves_bp:{rating:bp}}` |
| `GET /market/cds?as_of=` | котировки CDS |

### 4.4 Результаты расчётов и данные для мониторов

| Запрос | Ответ |
|---|---|
| `GET /runs?as_of=&status=&limit=` | прогоны |
| `GET /runs/<id>` | прогон, число строк, лог (предупреждения и ошибки) |
| `GET /desk?as_of=&run_id=` | **всё для мониторов одним объектом**: форма `window.DESK` (`docs/DATA_CONTRACT.md` §6) |
| `GET /desk-data.js?…` | то же в виде скрипта: так данные получают страницы |
| `GET /scenarios?kind=preset\|historical` | определения сценариев с описанием |

### 4.5 Служебное

| Запрос | Ответ |
|---|---|
| `GET /coverage?from=&to=` | `{datasets, required, days:[{date, eod_run, positions, funding, cash_balances, md_curves, md_fx_cbr, …}], days_with_gaps}`. Даты `md_fx_cbr` (есть и выходные) дней не добавляют |
| `GET /loads` | последние 200 загрузок файлов: статус, строки, ошибки |
| `GET /health` | `{status, version, latest_run}` |

---

## 5. Какие данные нужны каждому экрану

| Экран / функция | Результаты прогона | Входные и производные |
|---|---|---|
| **KPI-лента**: DV01, CS01, ОВП, PnL дня, MV | `position`, `position_fx`, `position_pnl`, `funding_*` | `desk_history` (спарклайны, «vs вчера») |
| **Сводка**: DV01 по кривым и бакетам, сегменты, топ эмитентов, PnL за период | `position_risk`, `position` | `ref_issuer` (группы), `desk_history` |
| **Позиции**: таблица и карточка | `position`, `position_risk`, `position_pnl` | `ref_issuer` (имя, группа, рейтинг на дату); условия бумаг и сделок, количество и цена покупки — через `/instruments`, `/otc-deals`, `/positions` (в карточку UI пока не выведены) |
| **Облигации**: таблица по выпускам, карта доходностей, история выпуска | `position` облигаций: `ytm`, `price`, `accrued`, `mdur`, `spr_dur`, `convexity`, `spread_bp` / `spread_type` / `spread_curve`, `yield_date`, MV, DV01, CS01 | `md_bond_quote` (Δ за день в `quote`, ряды — `/bonds/history`), `md_curve_point` (спред к выбранной кривой сегодня и в истории), `ref_instrument.offer_date`, `pos_snapshot` (позиция в истории) |
| **Риски по кривым**: теплокарта, лестница, кривая сегодня/вчера | `position_risk` | `md_curve_point` → пилларсы и изменения |
| **Экспозиции**: эмитент, сектор, рейтинг, валюта, класс; концентрации; ОВП | `position`, `position_fx` | `ref_issuer`, `ref_issuer_rating` (рейтинг на дату), `fund_snapshot` |
| **Фондирование**: строки по бумагам и остаткам, стоимость против бенчмарка, разрывы, справочник ставок, сценарии кэрри | `funding_deal`, `funding_pnl`, `funding_rule`, `run_ocp_transfer` | `md_fixing` (бенчмарки КС, RUONIA, SOFR, RUSFAR CNY, €STR, SARON), `ref_cbr_meeting`, `carry_scenario_def` |
| **Сценарии** | `position_risk`, CS01, FX-дельта, `position_gamma` / `convexity` (второй порядок), `scenario_result` | `scenario_def` (пресеты и исторические) |
| **PnL explain** | `position_pnl`, `funding_pnl` | `pnl_official`: если расчётный сервис не передал `official`, он берётся отсюда при публикации прогона (по `position_id`); `md_*` — сдвиги для матрицы rates explain |
| **Pivot Studio**: любые разрезы и меры | всё из прогона | справочники как измерения |
| **Scenario Lab**: конструктор, история explain | чувствительности, `desk_history` | `/market/curves` (уровни), исторические сценарии |

---

Сейчас страницы получают данные одним объектом (`/desk-data.js`): результаты прогона плюс снимок статики и рынка из входного слоя (`run.add_reference_data()`). Исключение — история выпуска: годовые ряды не кладутся в `/desk-data.js`, страница запрашивает `/bonds/history` по выбранному выпуску. Остальные эндпойнты §4.1–4.3 доступны другим потребителям и следующим версиям экранов (карточка инструмента, графики рядов кривых и курсов).

## 6. Потоки данных

### Ежедневно, после закрытия рынка (~19:00)

```
1. ваши выгрузки за день     → C:\tdm_inbox\*.csv
2. python -m tdm load C:\tdm_inbox\                     # срезы, рынок, официальный PnL, изменения справочников
3. python -m tdm coverage --from <сегодня>              # всё ли пришло
4. расчётный сервис: with Run(as_of=сегодня, run_type="EOD") as run:
       run.add_reference_data()                         # статика, рынок (+ курсы ЦБ), справочник ставок из tdm
       run.add_positions(...); run.add_funding(...)     # риски и PnL explain из либы
       run.derive_funding(); run.add_ocp_transfers()    # 1.3: недостающие строки фондирования, сводка передачи ОВП
       run.add_scenario_results(...)                    # полная переоценка (или позже: attach_scenario_results)
5. python -m tdm build-hist-scenarios                   # худшие исторические окна для нового портфеля
→ мониторы видят новый день (флэш по рыночным курсам) при обновлении страницы

На следующее утро (T+1), версия 1.3:
6. pnl_official_<T>.csv → python -m tdm load             # официальный управленческий PnL за T (курсы ЦБ, с фондированием)
7. python -m tdm attach-official --as-of <T>            # прикрепить к EOD-прогону за T; монитор покажет сверку
```

Шаги 2–5 удобно оформить одним `.bat` в Планировщике заданий (`docs/INSTALL_WINDOWS.md` §6).

### Первичная загрузка истории (1 год)

1. Справочники, затем рынок и срезы за год: `python -m tdm load C:\history\` — порядок соблюдается автоматически.
2. `python -m tdm coverage` — найти дыры и догрузить.
3. Результаты расчётов за прошлые дни, на выбор:
   - **(а)** расчётный сервис прогоняет EOD за каждый день года (`as_of` = дата): полная история рисков и explain, `desk_history` заполнится сама;
   - **(б)** только сегодняшний прогон, а итоги прошлых дней — файлом `desk_history.csv`.
4. `python -m tdm build-hist-scenarios`.

### Интрадей

Новый срез позиций в течение дня → `tdm load` (заменит срез за сегодня) → прогон `run_type="INTRADAY"`. Мониторы покажут его как последний. Вечером EOD-прогон заменит его в истории.

---

## 7. Качество и согласованность

- **Загрузка атомарна по файлу.** Ошибка в любой строке — файл не загружен, номера строк в отчёте и в `/loads`.
- **Срез полный по паре (дата, книга).** Позиции, которой нет в файле, нет и в срезе.
- **Прогон атомарен.** Проверки при публикации описаны в `docs/DATA_CONTRACT.md` §5.3.
- **Покрытие.** `/coverage` показывает пропуски обязательных наборов по датам до запуска расчётов и до построения исторических сценариев.
- **Воспроизводимость.** Прогон хранит снимок статики и рынка, на которых считался. Обновление справочника не меняет прошлые прогоны.
- **Источники.** В каждом ряду есть `source`. Правило «один источник на ряд на весь год» — в `docs/DATA_REQUIREMENTS.md` §2.

---

## 8. Не входит (пока)

| Что | Статус |
|---|---|
| Лимиты, VaR | считаются отдельными модулями, в модель не импортируются. При необходимости — отдельные таблицы с привязкой к `as_of` / `run_id` |
| Сделки в реальном времени | не нужны: работаем на ежедневных снэпшотах; интрадей — дополнительными срезами |
| Полная переоценка по запросу | спроектирована (`docs/SCENARIOS.md` §5, таблица `scenario_job`), не реализована |
| Запись через API | сейчас загрузка — `tdm load` / `tdm.writer` / `tdm import`, API только читает |
| Авторизация | сайт во внутренней сети без входа; AD / LDAP — отдельной задачей |
| Другая СУБД | SQLite хватает для деска; схема переносится на PostgreSQL / MS SQL почти один к одному |
