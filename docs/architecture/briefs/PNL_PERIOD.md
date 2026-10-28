# PnL за период: контракт API и экрана (TDM#386)

Задачи: API — TDM#407, экраны A/B/C — TDM#408, экраны D/E/F — TDM#409, сохранение входных данных flash — TDM#406.

Экран C19 «PnL за период» заменяет «Историю PnL» на том же адресе `c19-pnl-history.html`. Утверждённый макет экранов A–F
на синтетических данных — у владельца; суть каждого экрана описана ниже. Данные — сохранённые прогоны, как у
`/api/v1/history/pnl` (#67): основной EOD каждой даты до якоря, выбранный прогон на его дату. Методика PnL не меняется.

Правила для всех частей:

- Нет данных — `null` и понятный текст на странице («—», «нет данных», «N из M записей»). Никаких нулей вместо пропуска и
  никаких оценок вместо расчёта (05_FRONTEND_INTEGRATION §4a).
- Суммы — по исходным значениям (`Sum`), без округлённых промежуточных итогов. Итог строки — `amounts()` из
  `pnl_history.py` (существующая net-конвенция).
- Фильтры те же, что у `/api/v1/history/pnl`: `from`, `to`, `run_id`/`as_of`, `books[]`, `ports_off[]`, `entity`,
  `desk`, `ccy`, `asset_class`, `issuer`, `q`. Проверки периода — `periodError`/`MAX_PERIOD_DAYS` как сейчас.
- Старый `/api/v1/history/pnl` остаётся совместимым.
- Публичный репозиторий: только общие названия (ADR-008), в тестах синтетические книги и позиции.

## Компоненты

`keys` — порядок показа. Ключи `PNL_KEYS` + `reported`, плюс разбивка кэрри, когда она сохранена в `pnl.extra`
(FlyingScotman/trading-desk-monitor#406, stream:calc; до неё ключей нет):

| ключ | источник | на экране |
|---|---|---|
| `carry` | `pnl.carry` | Кэрри (купон + амортизация к номиналу, выплаты внутри) |
| `carry_coupon`, `carry_pull` | `pnl.extra.carry_coupon`, `pnl.extra.carry_pull` | Купон / НКД, Амортизация к номиналу; без них — «нет данных» |
| `rolldown` | `pnl.rolldown` | Roll-down |
| `funding` | `pnl.funding` | Фондирование |
| `rates`, `spread`, `convexity` | `pnl.*` | Ставки, Кредитный спред, Выпуклость |
| `fx_gross`, `fx_funding` | `pnl.*` | FX актива, FX фондирования |
| `new_trades` | `pnl.new_trades` | Сделки (во flash заглушка 0 — подписать) |
| `other` | `pnl.other` | Необъяснённое / прочее по методу |
| `reported` | `other` метода `desk-reported-v1` | Разница PV по отчётам |

Кэрри на экране: **нетто** = `carry + rolldown + funding`; **брутто** = `carry + rolldown`, фондирование отдельно.
На экране C: нетто = «кэрри без roll-down» (`carry + funding`) + `rolldown`; брутто = `carry_coupon` + `carry_pull`
(или `carry` целиком, если разбивки нет) + `rolldown`.

«Explain по кривым»: `rates` позиции относится к её кривой дисконтирования (`fact.spread_curve`; нет — `null` → «кривая
не указана»). Это точная переоценка позиции, отнесённая к одной кривой, а не разложение по кривым.

Атрибуция: Цена = итог − купон − FX нетто − фондирование − сделки; Купон = `carry_coupon`; Валюта = `fx_gross +
fx_funding`; Фондирование; Сделки. Без `carry_coupon` атрибуция «нет данных».

## `GET /api/v1/history/pnl/period`

Дополнительно к фильтрам: `gran=day|week|month` (обязателен), `catalogue=true|false` (как у старого API).

```
{
  from, to, gran, anchorRunId, currency: "RUB",
  marketDates: [date], runs: [{date, runId, prevDate, cutType, slice}],
  intradayOnlyDates: [date],
  keys: [key],
  curves: [{id, name}],                    // кривые, встретившиеся у позиций выборки
  buckets: [{key, label, from, to}],       // по gran, покрывают [from, to]; неделя с понедельника, месяц календарный
  summary: Agg,
  days: [Day],                             // только даты с сохранёнными строками, по возрастанию
  positions: [Pos],
  catalogue?: [...],                       // как у /history/pnl
  separateAnchor?: {...}                   // как у /history/pnl; в суммы периода не входит
}
Agg = {total, rows, noTotal, partialRows, grossCheck, grossCount,
       c: {key: number|null}, m: {key: int}}  // m — число строк без этого компонента
Day = Agg + {date, prevDate, runId, ratesByCurve: {curveId|"": number}}
Pos = Agg + {ref, book, positionId, name, instrument, ccy, kind, group, curve, rating, issuer,
             firstDate, lastDate, closed,      // closed: нет строки на последнюю дату периода с прогоном
             pvAvg, calDays, fundingRatePct,   // среднее pv_rub по строкам; дни интервалов; ставка фонд., взвеш. по телу
             buckets: [number|null]}          // итог позиции в каждом bucket, по индексу buckets
```

`ref` = `book + "|" + positionId` — пользовательская единица «инструмент в книге». `kind`/`group` — из
`publish.summary.kind_of` (группы и виды без названий организации). Группировки экрана по типу, книге, валюте,
рейтингу и позиции клиент собирает из `positions`.

## `GET /api/v1/history/pnl/curves`

Те же фильтры. Для экрана D.

```
{ from, to, firstRun, lastRun, buckets: [label], curves: [{id, name}],
  move:   {curveId: {bucket: bp|null}},     // сдвиг кривой между рыночными днями firstRun.prevDate и lastRun.date
  dv01:   {curveId: {bucket: rub|null}},    // среднее по прогонам fact.risk выборки
  linear: {curveId: {bucket: rub|null}},    // Σ pnl.extra.rates_linear_curves; null, пока не сохраняется
  linearTotal: {curveId: rub|null},         // Σ pnl.extra.rates_linear по позициям кривой
  exact:  {curveId: rub|null},              // Σ pnl.rates по позициям кривой
  coverage: {rows, linearRows, bucketRows} }
```

## `GET /api/v1/history/pnl/position`

`position_book`, `position_id` + период и якорь. Для экрана E.

```
{ position: {ref, book, positionId, name, instrument, ccy, kind, group, curve, rating, issuer},
  days: [{date, prevDate, runId, method, note, noPnl: {reason, detail}|null,
          total, c: {key: number|null}, ratesLinear, grossCheck,
          inputs: {source: "prev"|"previous_row"|null,
                   cleanPrev, clean, ytmPrev, ytm, zPrev, z, dv01Prev, dv01, cs01,
                   quantityPrev, quantity, pv},
          events: [str]}] }
```

`inputs.source`: `prev` — из `pnl.extra.prev` (после TDM#406), `previous_row` — из строки предыдущего прогона
той же позиции (подписать «по прогону D−1»; при другом количестве поля D−1 — `null`). `z`/`zPrev` — `extra.z1_bp`/`z0_bp`.
`events`: появление, исчезновение, смена количества, `no_pnl`, оферта/погашение из fact, если есть.

## `GET /api/v1/history/pnl/checks`

Те же фильтры + `thr_abs` (RUB, по умолчанию 3e6) и `thr_pct` (доля от суммы модулей объяснённого, по умолчанию 0.25).

```
{ checks: [{key, label, status: "ok"|"findings"|"no_data", count, note}],
  findings: [{sev: "crit"|"warn"|"info", check, date, dateTo, ref, book, positionId, name, value, text}],
  coverage: {marketDates: [date], runDates: [date], missingRuns: [date]},
  unexplained: {dates: [date], refs: [ref], cells: [[dateIdx, refIdx, value]]} }
```

Проверки (`key`): `recon` (|grossCheck| > 1000 RUB), `unexpl` (|other| ≥ thr_abs и ≥ thr_pct × Σ|объяснённого|), `stale`
(чистая цена не менялась 3+ прогона подряд при ненулевом DV01, или `quality == stale_price`), `fsign` (знак funding
совпадает со знаком pv), `lin` (|rates − rates_linear| > max(3e5, 5% |rates_linear|)), `fxrev` (курс валюты позиций
сдвинулся > 2% и вернулся > 2% на следующий прогон), `outlier` (|итог − медиана| > 7 MAD позиции за период), `norun`
(рыночный день без прогона), `life` (появление, закрытие, смена количества, `no_pnl`). Нет входных данных для проверки —
`no_data` с причиной, а не `ok`.

## Экран

`c19-pnl-history.html` грузит `lib/pnl-period.js` (чистые функции, UMD, тесты node), затем файлы экранов
`lib/pnl-period-curves.js`, `lib/pnl-period-position.js`, `lib/pnl-period-control.js`, затем `lib/pnl-period-page.js`
(оболочка и экраны A, B, C). Экран регистрируется так:

```
window.PNL_PERIOD_SCREENS = window.PNL_PERIOD_SCREENS || {};
PNL_PERIOD_SCREENS.curves = { render(main, ctx) { ... }, dispose() { ... } };
// ctx = { K, P: PNL_PERIOD, state, period /* ответ /period */, query(extra) -> URLSearchParams с периодом и фильтрами,
//         fetchJson(path, params, signal) -> Promise, openPosition(ref, date), rerender() }
```

Вкладки по hash: `#decomp` (A), `#matrix` (B), `#carry` (C), `#curves` (D), `#position` (E), `#control` (F).
Общие период (День, Неделя, MTD, 3М, Квартал, YTD, свой), книги и фильтры — через `K.filter` и шапку страницы.
Цвет и название источника одинаковы на всех экранах (`PNL_PERIOD.COMPONENTS`). Числа — `K.f` с `K.negFmt`; таблицы с
`data-vk`, чтобы работали `K.tableToolsAll`.

- **A · Декомпозиция.** Плитки: итог, кэрри нетто, ставки + спред, FX нетто, необъяснённое. Водопад в режимах Explain /
  Explain по кривым / Атрибуция / Типы / Книги / Валюты / Позиции, галочка «кэрри нетто» (включена по умолчанию; без
  неё брутто и фондирование отдельно). Щелчок по столбцу — список позиций, давших вклад (добавили / отняли). График
  «Накопленно / По периодам» в том же разрезе, Дни / Недели / Месяцы. Таблица «По периодам» со своим выбором разбивки
  (те же режимы + рейтинг); строки позиций ведут в E.
- **B · Матрица.** Строки: тип / книга / валюта / кривая / рейтинг → позиция; столбцы — компоненты Explain или
  Атрибуции; подсветка, сортировка, свернуть/раскрыть. Щелчок по ячейке — её накопление и крупнейшие дни (данные по
  дням позиции — `/position`).
- **C · Кэрри.** Плитки купона, амортизации, roll-down, брутто, фондирования, нетто (+ % годовых на средний pv).
  Графики «накопленно» и «в день на календарный день» с переключателем Нетто / Брутто и отметками решений по ставке
  (`md_cbr_meeting`, если отдаётся; иначе без отметок). Таблица по позициям с группировкой: pv ср., компоненты, кэрри
  % год., фондирование % год., маржа bp.
- **D · Ставки по кривым.** Три таблицы кривая × срок: сдвиг bp, DV01 средний, PnL ставок линейно; накопленный PnL
  ставок по кривым (из `/period` ratesByCurve); линейная против точной по кривым.
- **E · Позиция.** Список позиций выборки с итогом и отметкой находок; шапка (инструмент, книга, тип, валюта, кривая,
  статус); плитки; атрибуция «цена / купон»; накопление по компонентам или по дням; цена и доходность отдельными
  графиками; таблица по дням с входными данными, компонентами, линейными ставками, «переоценка − Σ» и находками.
- **F · Контроль.** Пороги, карточки проверок со статусом, календарь покрытия, тепловая карта необъяснённого «позиции ×
  дни», список находок; каждая находка открывает E на нужный день.

Ограничения метода из нынешней C19 (не официальный PnL, сделки не восстанавливаются, фондирование уровня книги не
входит и т. п.) остаются на экране одним сворачиваемым блоком.

## Уточнения реализации

API (TDM#407: `backend/src/desk/api/pnl_period.py`, `pnl_checks.py`). Поля контракта не менялись; ниже — как
прочитаны неоднозначные места и что добавлено сверх контракта.

- **Компоненты.** `c` и `m` всегда содержат все ключи, включая `carry_coupon`/`carry_pull` (`null`, пока не сохранены);
  `keys` перечисляет разбивку кэрри, только если она есть хотя бы в одной строке выборки. `m` считает строки, где
  компонента применима: как у `/history/pnl`, строка метода `desk-reported-v1` не считается пропуском `other`, а
  строка flash — пропуском `reported`.
- **Buckets.** `day` — календарные дни `[from, to]`; `key`: `2026-10-28` / `2026-W44` (ISO-неделя) / `2026-10`;
  `label`: `28.10.2026` / `26.10–01.11.2026` / `окт 2026`; `from`/`to` обрезаны по периоду. `Pos.buckets[i]` — `null`,
  если в bucket нет строки с итогом.
- **Pos.** Атрибуты — по последней строке позиции; `kind`/`group` — `kind_of` (эмитенты `is_gov` из справочников
  прогонов периода); `rating` — `fact.extra.rating.grade` (без отката на рейтинг эмитента); `pvAvg` — среднее
  `run_position.pv_rub` по строкам, где он есть; `calDays` — Σ (дата − `prevDate`) по строкам с итогом (`prevDate` —
  `pnl.extra.prev_date`, иначе прогона), `null` без таких строк; `fundingRatePct` — `rate_pct` линий
  `fact.extra.funding.lines`, взвешенный по |`body_ccy`| × `days`.
- **Сверх контракта в `/period`:** `cbrMeetings: [{date, effectiveDate, decision, ratePct, prevRatePct, estimated}]` —
  решения `md_cbr_meeting`, вступающие в силу в периоде (для отметок экрана C; без данных — пустой список).
  `separateAnchor` — `{run: {date, runId, prevDate, cutType, slice, …}, overlappingDates, summary, days, positions}`
  в формах `Agg`/`Day`/`Pos`. `/curves`, `/position` и `/checks` отдельный якорь не учитывают (в `coverage.runDates`
  проверок его дата есть).
- **`/curves`.** `buckets` — сроки риска (порядок `RISK_BUCKETS`): из `fact.risk` выборки и точек кривых последнего
  прогона. `curves` — кривые из `fact.risk`, `fact.spread_curve` и `rates_linear_curves`. `move` = ставка последнего
  прогона − (ставка первого − его `move_bp`), bp; `null`, если у кривой нет точки. `dv01` — Σ `fact.risk` ÷ число
  прогонов, где у выборки есть строки. `linearTotal`/`exact` — по `fact.spread_curve`, `""` — кривая не указана.
  `firstRun`/`lastRun` — `{date, runId, prevDate}` или `null`.
- **`/position`.** Фильтры выборки тоже применяются; нет строк — `200 {position: null, days: []}`. `previous_row` —
  строка той же позиции только в непосредственно предыдущем прогоне периода; при другом количестве `cleanPrev`,
  `ytmPrev`, `dv01Prev` — `null`, `quantityPrev` остаётся (по нему видно смену). `events` — строки: «позиция
  появилась», «позиция снова в прогоне после …», «количество изменилось», «нет PnL: <reason>», «оферта ДД.ММ.ГГГГ»,
  «погашение ДД.ММ.ГГГГ» (дата в (`prevDate`, `date`]), «позиции нет в прогоне ДД.ММ.ГГГГ».
- **`/checks`.** В ответе также `from`, `to`, `anchorRunId`, `thrAbs`, `thrPct`. `sev`: `crit` — `recon`, `fsign`;
  `warn` — `unexpl`, `stale`, `lin`, `fxrev`, `norun`; `info` — `outlier`, `life`. Находки отсортированы по `sev`,
  затем по дате. `value`: `recon` — grossCheck, `unexpl` — other, `stale` — цена (у находки по `stale_price` — `null`),
  `fsign` — funding, `lin` — rates − rates_linear, `fxrev` — доля сдвига курса, `outlier` — итог дня, `life` —
  количество при его смене. `fxrev` — находка по валюте: `ref`/`book`/`positionId` — `null`, `name` — код валюты,
  `date`/`dateTo` — день скачка и день возврата. `norun` — `ref` `null`; дата с одним внутридневным прогоном
  подписана отдельно. `stale` даёт две находки: серия одинаковой цены (`date`…`dateTo`) и серия `stale_price`.
  `fsign` смотрит строки с ненулевыми funding и pv. `outlier` — позиции с 5+ днями итога; при MAD = 0 не проверяется.
  `life` дополнительно отмечает пропуск прогона внутри периода. `unexplained.cells` — строки, где `other` сохранён
  (нули включены); `refs` — позиции с такими строками.
