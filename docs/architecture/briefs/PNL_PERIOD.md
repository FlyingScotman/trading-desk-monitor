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
| `fx_gross`, `fx_funding` | `pnl.*` | Только вместе: FX (нетто ОВП) = `fx_gross + fx_funding` — см. ниже |
| `new_trades` | `pnl.new_trades` | Сделки (во flash заглушка 0 — подписать) |
| `other` | `pnl.other` | Необъяснённое / прочее по методу |
| `reported` | `other` метода `desk-reported-v1` | Разница PV по отчётам |

Кэрри на экране: **нетто** = `carry + rolldown + funding`; **брутто** = `carry + rolldown`, фондирование отдельно.
На экране C: нетто = «кэрри без roll-down» (`carry + funding`) + `rolldown`; брутто = `carry_coupon` + `carry_pull`
(или `carry` целиком, если разбивки нет) + `rolldown`.

«Explain по кривым»: ставки по факторам `ratesByFactor` (TDM#437) — RUONIA, базис ОФЗ (КБД − RUONIA), КБД (нет RUONIA
на дату), прочие кривые, «кривая не указана». Сервер без `ratesByFactor` — по-старому: `rates` позиции относится к её
кривой дисконтирования (`fact.spread_curve`; нет — `null` → «кривая не указана»), это точная переоценка позиции,
отнесённая к одной кривой, а не разложение по кривым.

Валютный PnL показывается только нетто по ОВП (решение владельца 28.10.2026): один источник «FX (нетто ОВП)» =
`fx_gross + fx_funding`. Переоценка валютного актива и валютного пассива по отдельности на экране не показывается
нигде: ни в строках, ни в плитках, ни в подсказках. API отдаёт обе части как сырые данные. Нетто `null`, только если
обе части `null`; если есть одна, показывается она, а строка считается неполной.

Атрибуция: Цена = итог − купон − кэрри без разбивки − FX нетто − фондирование − сделки; Купон = `carry_coupon`;
Кэрри без разбивки купона = `carry_nosplit`; Валюта = `fx_gross + fx_funding`; Фондирование; Сделки. Разбивка — по
строкам (TDM#432): строка без `carry_coupon` отдаёт кэрри целиком в «Кэрри без разбивки купона» и не гасит остальные.

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
  coverage: {rows, linearRows, bucketRows},
  // по факторам (TDM#437): factorId — "ruonia" | "basis" | "kbd" | id другой кривой | "" (кривая не указана)
  factors: [{id, name}],
  factorMove: {factorId: {bucket: bp|null}}, factorDv01: {factorId: {bucket: rub|null}},
  factorLinear: {factorId: {bucket: rub|null}}, factorLinearRest: {factorId: rub|null},
  factorLinearTotal: {factorId: rub|null}, factorExact: {factorId: rub|null},
  factorCoverage: {factorId: {rows, linearRows, bucketRows, dates}} }
```

Факторы ставок (TDM#437) вместо кривых «КБД» и «RUONIA»: «RUONIA» — позиции с дисконтированием по RUONIA OIS и
RUONIA-часть ОФЗ; «Базис ОФЗ (КБД − RUONIA)» — ОФЗ в модели RUONIA + базис (TDM#213); «КБД (нет RUONIA на дату)» —
строки на КБД без разбивки (дата без RUONIA OIS, а также прогоны до TDM#213); другие кривые (`RUB KEY`, валютные) —
факторами под своим id. Сумма факторов равна сумме по кривым (линейной и точной).

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
  unexplained: {dates: [date], refs: [ref], cells: [[dateIdx, refIdx, value]]},
  unexplainedBy: {total, other, reported, rows,          // TDM#436: Σ other + reported периода
                  groups: [{key, label, total, other, reported, rows}],
                  methods: [{method, group, total, rows, quality: {quality: rub}}],
                  quality: [{quality, label, total, rows}],
                  dates: [date], byDate: {groupKey: [rub|null по dates]},
                  reasons: [{key, label, total, other, reported, rows, positions,     // TDM#442: по причине
                             methods: [{method, group, total, rows}],
                             top: [{ref, book, positionId, name, total, rows}]}],
                  byReasonDate: {reasonKey: [rub|null по dates]},
                  top: [{ref, book, positionId, name, total, other, reported, rows, group, groupLabel,
                         reason, reasonLabel, reasons, reasonDetails,
                         methods, quality, notes, noPnl: [{reason, text, days}]}], topTotal} }
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
- **Ключи сверх `PNL_KEYS` (TDM#432, TDM#435).** `carry_nosplit` — Σ `carry` строк с кэрри без `carry_coupon` (методы
  flash-simple / flash-yield, пересчёт без даты); `m` у него всегда 0 (не пропуск), `null` — таких строк нет. `rates_linear`
  — Σ `pnl.extra.rates_linear` облигаций (`instr_type` BOND или нет — как `kind_of`; в любой валюте: у валютной
  линейная оценка в RUB по курсу D, как `rates`, TDM#418), `rates_exact_lin` — Σ `rates` тех же строк; `m` обоих — строки без
  линейной оценки. В `keys` — только когда есть в выборке, после `carry_pull` и после `rates`. В `/position` дни получают
  `carry_nosplit` (линейные ставки у дня свои — `ratesLinear`).
- **Pos сверх контракта:** `methods` — методы строк позиции (`pnl.extra.method`, по алфавиту), `reasons` — коды
  причин необъяснённого её строк (`pnl.extra.other_reason.code`, по алфавиту; строка с `other`/`reported` без кода —
  `unrecorded`; TDM#442), `noPnlReasons` —
  `{reason: число строк}` строк без PnL. Для списка «кто дал» на A (метод, причины «не посчитано»). Компоненты позиции
  за период — её `c`; отдельного списка «топ-N по компоненте» нет: `positions` уже содержит все позиции выборки.
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
  `life` дополнительно отмечает пропуск прогона внутри периода. Смена количества и `no_pnl` с той же причиной в
  прогонах подряд — одна находка: `date`…`dateTo`, в тексте «дней подряд: N» (`value` — последнее количество).
  `unexpl`: FX в объяснённом один раз, нетто (|fx_gross + fx_funding|, TDM#414), а не модули частей.
  `unexplained.cells` — строки, где `other` сохранён (нули включены); `refs` — не больше 60 позиций (`HEAT_REFS`) с
  наибольшим max |other| за период, по алфавиту; `refsTotal` (сверх контракта) — число позиций с такими строками до
  отсечения; `cells` и `dates` — только по оставленным. Строки читаются по позициям: в памяти одна позиция.
- **`/curves` по факторам (TDM#437).** Строка ОФЗ в RUONIA + базис — та, где сохранены `rates_linear_ruonia` и
  `rates_linear_basis`. Линейно по срокам: член RUONIA = `prev.ofz_ruonia_risk[b]` × `curve_moves_bp["RUB RUONIA"][b]`,
  базис = член КБД (`rates_linear_curves["RUB OFZ"][b]`) − член RUONIA (срок без члена КБД — минус член RUONIA). Нет
  риска или сдвига RUONIA, или члены RUONIA расходятся с `rates_linear_ruonia` больше чем на 1 RUB (`SPLIT_TOL_RUB`) —
  строка идёт одной суммой без сроков: `rates_linear_ruonia` / `rates_linear_basis` → `factorLinearRest`. Строка без
  `rates_linear_curves` — `rates_linear` целиком на фактор своей кривой, тоже в `factorLinearRest`. `factorLinearTotal`
  = сроки + без сроков. Точно (`factorExact`): у ОФЗ RUONIA = `rates_linear_ruonia`, базис = `rates − rates_linear_ruonia`
  (базисный член и вся нелинейность полной переоценки), у остальных `rates` целиком на фактор `fact.spread_curve`.
  DV01: у строки ОФЗ с `extra.ofz_ruonia01` (риск в координатах сценариев: `RUB OFZ` — базис, `RUB RUONIA` — RUONIA минус
  базис) RUONIA = сумма обеих кривых, базис = `RUB OFZ`, как вид «RUONIA + базис» страниц; иначе кривая → фактор.
  Сдвиг: RUONIA — кривой RUONIA, базис — сдвиг КБД − сдвиг RUONIA, `kbd` — КБД. `factorCoverage[f]` — строки, давшие
  фактору точное или линейное значение, из них с линейным, по срокам, и число дат.
- **`/period` `ratesByFactor` (TDM#437, сверх контракта).** У `Day` и `Pos` (и в `separateAnchor`) — `rates` по
  факторам по тому же правилу, что `factorExact`; сумма равна сумме `ratesByCurve`. `ratesByCurve` не менялся.
- **`unexplainedBy` (TDM#436).** Σ `other` + `reported` строк периода (у строки есть одно из двух по методу) по группе
  метода `pnl.extra.method` (`METHOD_GROUP`): `exact` — «остаток точного flash» (`desk-flash-exact-*`), `simple` — «без
  разложения: нет потоков ядра» (`desk-flash-simple-v1`), `yield` — «по доходности»
  (`desk-flash-yield-v1`), `reported` — «по отчёту» (`desk-reported-v1`), `fx` — «FX-сделки: прочие эффекты»
  (`desk-fx-spot-v1`, `desk-fx-core-v1`), `other` — другой код или нет метода; `groups` — в этом порядке, только
  встретившиеся. Качество — `run_position.quality` даты D (`exact`, `assumed`, `market_value`, `stale_price`,
  `model_price`, `par_proxy`, `reported_value`; качество D−1 не хранится, оно только в тексте `note` простого PnL).
  `byDate[g][i]` — Σ группы на `dates[i]`, `null` без строк. `top` — не больше 30 позиций (`UNEXPL_TOP`) с наибольшим
  |Σ| за период, по убыванию; `group` — группа с наибольшим |Σ| у позиции; `methods`, `quality` — до 8 кодов,
  `notes` — до 3 разных `pnl.extra.note` (по 200 знаков), `noPnl` — причины дней без PnL позиции (до 5) с текстом
  `NO_PNL_TEXT` и числом дней. Различных кодов метода и качества — не больше 40 (`UNEXPL_KEYS_CAP`), остальные — `…`.
  Нет ни одной строки с `other` или `reported` — `total` `null`, списки пустые.
- **`unexplainedBy.reasons` (TDM#442).** Первый разрез: Σ `other` + `reported` по коду `pnl.extra.other_reason.code`
  (ниже, «Причина необъяснённого»), в порядке `pnl.reasons.OTHER_REASON_TEXT`, неизвестный код — после них как есть,
  последней — `unrecorded`: строка с ненулевым `other`/`reported` без кода, «причина не записана (прогон до версии 23)»
  (`NO_REASON_TEXT`, `REASONS_SINCE`). Строка без кода с нулём (FX-строка: всё в `fx_gross`) — ни в какой причине: Σ
  причин = `total`, Σ их `rows` ≤ `rows`. У причины — `methods` (второй разрез: метод и его группа `METHOD_GROUP`),
  `positions` — число позиций, `top` — до 5 позиций (`UNEXPL_REASON_TOP`) с наибольшим |Σ| этой причины.
  `byReasonDate[k][i]` — Σ причины на `dates[i]`. В `top` позиций: `reason` — причина с наибольшим |Σ| у позиции,
  `reasons` — до 8 кодов в порядке строк, `reasonDetails` — до 3 разных `other_reason.detail`. Группы метода
  (`groups`, `byDate`, `methods`) — как раньше, вторым разрезом. `METHOD_GROUP` берёт коды методов из их модулей
  (`pnl.flash.METHOD` / `METHOD_FX`, `valuation.fallback.METHOD` / `YIELD_METHOD`, `pnl.reasons`; TDM#452).
- **`/position` `days[].reason` (TDM#442).** `pnl.extra.other_reason` дня как есть, `null` без него.

## Уточнения реализации (экран)

Оболочка и экраны A, B, C (TDM#408: `lib/pnl-period.js`, `lib/pnl-period-page.js`).

- **ctx экрана.** `{K, P, state, period, prep, query(extra), fetchJson(path, params, signal), openPosition(ref, date),
  rerender()}`. `period` — ответ `/period` как есть; `prep` — `P.prepare(period)` (дни по возрастанию, у позиций
  `ratesByCurve`, `bucketAggs`, `dates` оси, `split`, `curveName(id)`). `query(extra)` — период, `run_id` и фильтры
  (`books`, `ports_off` повторяющимися ключами); `gran` не входит, его добавляет вызывающий; `extra` — значение, массив
  или `null` (убрать ключ). `fetchJson('/api/v1/history/pnl/…', q, signal)` — ошибка с текстом для страницы.
  `state.position` / `state.positionDate` — выбор для E (`openPosition`), `state.ui` — место для D/E/F. Оболочка
  вызывает `dispose()` перед каждой перерисовкой и при загрузке; пока идёт загрузка, экран не рисуется.
- **«Авто»** — по числу будних дней `[from, to]` (≤ 25 — день, ≤ 95 — неделя, иначе месяц): рыночные дни известны
  только из ответа, а `gran` — параметр запроса. Смена Дни / Недели / Месяцы — новый запрос.
- **Пресеты** — от даты выбранного прогона: Неделя — 7 календарных дней, 3М — с того же числа три месяца назад + 1 день,
  Квартал — с начала текущего квартала.
- **FX** — один источник «FX (нетто ОВП)» (`P.BY_KEY.fx.parts`); `null`, только если обе части `null`; покрытие — по
  менее покрытой части.
- **Разбивка кэрри — по строкам** (TDM#432, одно правило для A–E, `P.anySplit` / `P.noSplit`): строки с
  `carry_coupon` дают купон и амортизацию, кэрри остальных — `carry_nosplit` целиком («Кэрри без разбивки купона»);
  купон + амортизация + без разбивки = кэрри. Одна строка без разбивки больше не гасит остальные. Покрытие купона —
  «N из M записей», M — строки с `carry` (`P.splitCover`); строка без PnL не имеет `carry` и в M не входит. «Без
  разбивки» при 0 (разбивка у всех строк) не показывается. Ответ без `carry_nosplit` (старый сервер) при смешанной
  разбивке — «нет данных», при полном отсутствии разбивки — кэрри целиком. `P.hasSplit` / `P.hasCoupon` («у всех
  строк») остались для проверок.
- **Кэрри.** «Компоненты» на A/B и «Брутто» на C при разбивке хотя бы у одной строки — купон, амортизация, кэрри без
  разбивки, roll-down; без разбивки вообще — кэрри целиком. Плитки C: купон и амортизация строк с разбивкой с подписью
  «разбивка у N из M записей с кэрри», отдельная плитка «Кэрри без разбивки купона», если не 0; столбец «Без разб.» в
  таблице позиций — так же. Группы C — суммы, без условия «разбивка у всех».
- **Атрибуция** (A, B, E): Цена = итог − купон − кэрри без разбивки − FX нетто − фондирование − сделки; столбец «Кэрри
  без разбивки купона» между купоном и FX. Тождество с итогом выполняется: компонент, пропущенный в строке, отсутствует и
  в её итоге. Покрытие купона — рядом с названием строки таблицы «По периодам», в заголовке столбца B и в подсказке
  водопада.
- **Ставки облигаций** (TDM#435, TDM#418): в Explain (A, B) «Ставки» — три строки: «Ставки (линейно)» = `rates_linear`,
  «Выпуклость (ставки)» = `rates_exact_lin − rates_linear`, «Ставки (без линейной оценки)» = `rates − rates_exact_lin`
  (деривативы, записи без оценки; при 0 не показывается); сумма = `rates`. Покрытие линейной оценки —
  «N из M записей» строк со ставками. Нет `rates_linear` в `keys` — одна строка «Ставки». «Explain по кривым» — ставки
  по фактору (или по кривой) целиком. Выпуклость метода (`convexity`, во flash 0) называется «Выпуклость метода» и при 0 не показывается.
  Плитка «Ставки + спред» на A показывает линейную часть и выпуклость.
- **Разбивка столбцов водопада** (TDM#431): «нет / Валюта / Тип / Книга» для Explain, по кривым и Атрибуции (у
  разрезов-групп выключена). Доля группы в столбце — та же строка на сумме `Agg` позиций группы (`P.stackItems`); группы
  — по Σ |долей| по столбцам, до 7 и «Остальные N» (цвета `KIT.PAL` по порядку, легенда под графиком). Части одного
  знака складываются от начала столбца вверх (+) и вниз (−), черта поперёк — итог столбца; подпись — итог; остаток,
  который группы не покрывают (правило null), — серая часть «не разнесено по группам». Щелчок по столбцу — список
  позиций столбца, по части — позиций её группы.
- **Список «кто дал»** (TDM#431, TDM#433): позиции по |вкладу| за период (до 20, остальные строкой «ещё N поз. · Σ»), под
  каждой — методы расчёта (`Pos.methods`); вклады ≤ 5 тыс. RUB (`MIN_CONTRIB`) — строкой с числом и суммой; нулевые — не
  в списке, только счётчик; без значения — последней строкой «не посчитано: N поз.» с причинами («нет PnL:
  `noPnlReasons`» или «метод без этой компоненты: методы»). Щелчок по позиции в любом списке C19 открывает E.
- **null последними** (TDM#433): в водопаде разрезов-групп позиции без итога — одним столбцом «не посчитано: N поз.» в
  конце; в группах, таблице кэрри C и матрице сортировка ставит `null` в конец.
- **Ось по дням** (TDM#434, `P.periodDates`, `P.dayAxis`): рабочие дни, а не календарь — дни прогонов и строк плюс
  рыночные даты с понедельника по пятницу (в ответе нет рыночных дат — все будни); выходной попадает на ось только с
  прогоном. Рабочий день без прогона — полоса «нет прогона» (прогон без строк выборки — «нет записей»); накопленное на
  нём не меняется (`P.cumulate(vals, hold)`), подсказка говорит об этом. В шапке «Как накапливалось» — «прогонов N из M
  рабочих дней». При `gran=day` столбцы (`buckets`) сервера по календарным дням сужаются до дней оси, `Pos.buckets` —
  по тем же индексам (`P.prepare`); так же графики «По периодам» и таблица «По периодам». То же на графиках C.
- **«Накопленно» для разрезов-групп** (типы, книги…) — по столбцам периода (`Pos.buckets`): дней по группам в ответе нет.
- **Ячейка группы на B** — крупнейшие позиции группы; дни — только у ячейки позиции (`/position`, ответ кешируется на
  срез).
- **% годовых на C** — на `pvAvg` за `calDays` позиции; плитки — на Σ `pvAvg` позиций с pv за Σ интервалов дней;
  у сделок (`group` DER, MM) ставки в % не считаются. Фонд. % позиции — `fundingRatePct`; нет его — «—», без оценки
  по funding / pv.
- **Решения по ставке** (`cbrMeetings`) — отметка на первой дате оси не раньше `effectiveDate`.
- **Цвета** — семейства `KIT.PNL_COLORS` (кэрри, фондирование, ставки, спред, FX, сделки, прочее), как на других
  PnL-мониторах; «Разница PV по отчётам» и «Цена» — из `KIT.PAL`.

Экраны D, E, F (TDM#409: `lib/pnl-period-curves.js`, `lib/pnl-period-position.js`, `lib/pnl-period-control.js`; чистые
функции — в `lib/pnl-period.js`).

- **Запросы.** Каждый экран — свой `PNL_HISTORY.loader` (последний запрос выигрывает, `dispose` его отменяет); ответы
  кешируются в `state.ui.cache` по строке запроса (период, фильтры, позиция, пороги), поэтому смена темы или вкладки не
  спрашивает снова. Пока идёт запрос, на месте данных — «Загружаю…», прежний ответ не показывается.
- **D по факторам (TDM#437).** Вид по умолчанию — «Факторы» (переключатель «Факторы / Кривые», `state.ui.cvView`;
  без `factors` в ответе — только кривые): три таблицы фактор × срок (`P.factorGrids`), у линейной — столбец «без
  сроков» (`factorLinearRest`), Σ = `factorLinearTotal`; у строки «КБД (нет RUONIA на дату)» — покрытие «N записей · M
  дат». Накопленный PnL — из `ratesByFactor` дней (`P.factorSeries`), линейная против точной — `P.factorLinVsExact`
  (предупреждение — при полном линейном покрытии фактора), позиции — по строке на пару позиция × фактор
  (`P.factorPositions`).
- **A · «Explain по кривым» по факторам (TDM#437).** Есть `ratesByFactor` в днях — `componentRows('curves', {prep})`
  строит ставки из `P.factorRows(prep)` (строки `rates:f:<id>`), иначе — по кривым. Итог периода и столбцы «По периодам»
  — `P.factorAgg` дней (`P.prepare`, `P.sumAggs`); список «кто дал» и разбивка столбцов (TDM#431) — `ratesByFactor`
  позиций и сумм групп. Σ факторов = `rates`.
- **A · «Необъяснённое» по причине, затем по методу (TDM#436, TDM#442).** Щелчок по столбцу «Необъяснённое / прочее» или
  «Разница PV по отчётам» добавляет под списком «По причине» и «По методу расчёта» (`P.unexplainedSplit`: `reasons` и
  `groups`): если ответ `/checks` этого периода и фильтра уже в кеше экрана F — `unexplainedBy.reasons` и группы по
  записям (своего запроса A не делает); иначе — по позициям из `/period` (`Pos.reasons`; `Pos.methods` → группа как
  `pnl_checks.METHOD_GROUP`; у позиции с разными причинами — «позиции с несколькими причинами», с методами разных групп
  — «позиции с несколькими методами», без кодов — «причина не записана»). Для части столбца (разбивка по группам) —
  только по позициям. Ссылка «подробнее — экран F». Зеркало `METHOD_GROUP`, `UNEXPL_GROUPS` и названий причин в
  `lib/pnl-period.js` (`P.METHOD_GROUP`, `P.UNEXPL_LABEL`, `P.REASON_LABEL`, `P.REASON_ORDER`) сверяет с исходниками
  бэкенда тест `tests/js/pnl_period_factors.test.js` (TDM#452).
- **D.** Сдвиг — bp со знаком («+12» / «−15», без бухгалтерских скобок); подсветка расходящаяся: зелёный — снижение
  ставок, длинный DV01 (< 0, DATA_CONTRACT), прибыль. Линейный PnL по срокам без `rates_linear_curves` — «нет данных» с
  покрытием `coverage.bucketRows`. Предупреждение «линейная против точной» — |разница| > 3 % линейной и > 1 млн RUB и
  только при `linearRows == rows`. Таблица позиций — точные ставки и доля кривой: разбивки позиции по срокам в `/curves`
  нет.
- **E.** Позиция по умолчанию — наибольшая по |итогу| выборки. ▲ в списке и полосы строк — находки `/checks` с порогами F;
  находка покрывает дни `[date, dateTo]`; `fxrev` (без `ref`) относится к позициям в этой валюте. «Переоц. − Σ» —
  `grossCheck`. Колонки компонент — с значением хотя бы в один день; FX — одна колонка нетто.
- **F.** Пороги (млн RUB, % объяснённого) → `thr_abs`, `thr_pct`, новый запрос. Календарь — рыночные дни и дни прогонов:
  нет прогона / находки выше «инфо» (кроме самой `norun`) / в норме. Шкала карты необъяснённого — до порога, не меньше
  0,1 млн; щелчок по клетке и по находке позиции открывает E на этот день. На карте не больше 60 позиций
  (`P.HEAT_ROWS`, наибольшие по |other|) с подписью «показаны 60 из N позиций с наибольшим необъяснённым»; N —
  `unexplained.refsTotal`, если сервер его отдаёт, иначе число позиций в ответе.
- **F · Необъяснённое (TDM#436, TDM#442).** Блок «Необъяснённое: что внутри» после календаря (`P.unexplainedView`):
  полосы по причине (цвет `P.REASON_COLOR`; в подсказке — разбивка причины по методам и её крупнейшие позиции; щелчок —
  в таблице только позиции с этой причиной, `state.ui.whyReason`), затем по группам метода (`P.UNEXPL_COLOR`), суммы по
  качеству оценки D, итог и проверка «разбивка сходится с итогом» (по причинам; ответ без `reasons` — по методам);
  столбцы по дням, сложенные по причинам (без `reasons` — по группам); таблица крупнейших позиций (причина, метод,
  качество, примечание: `reasonDetails` и `notes`; щелчок — позиция на E). Нет `unexplainedBy` или `total` `null` —
  «нет данных».

## Причина необъяснённого (TDM#442)

PnL, который нельзя разложить, остаётся в `other` (или `reported`), без приближений (решение владельца от 29.10.2026).
Каждая запись с такой частью пишет в `pnl.extra.other_reason` `{code, detail, …}` (`pnl.reasons.other_reason`;
определения — docstring `pnl/flash.py` и `pnl.reasons.OTHER_REASON_TEXT`). В копию extra у факта (`tdm_run.pnl_extra`)
не попадает (`pnl.flash.STORE_ONLY_KEYS`). Записи до версии расчёта 23 кода не имеют.

| `code` | Путь | Поля сверх `code`, `detail` |
|---|---|---|
| `flash_residual` | точный flash (`desk-flash-exact-v2`, `-fx-v1`): остаток `other` | — (`detail`: сменились горизонт или расписание, иначе цена и НКД D) |
| `price_source_switch` | модельная цена на одну дату, рыночная на другую: на ядре — точные шаги, шаг спреда (Z модели против Z рынка) — открытие цены, в `other`; вне ядра — простой PnL | `spread_rub`, `residual_rub` (на ядре), `rung` (вне ядра) |
| `discount_curve_switch` | кривая дисконтирования D−1 и D разная (КБД в день без RUONIA OIS): простой PnL | `curves` [D−1, D], `curve`, `date` — кривая, которой нет на дату, если известна |
| `no_curve_on_date` | ядро отказало из-за кривой, которой нет на дату (флоатер в день без RUONIA OIS, `core_missing_market_data`): запасная ступень, простой PnL | `curve`, `date`, `rung`, `core_code` |
| `core_refused` | ядро отказало по другой причине: запасная ступень, простой PnL | `core_code`, `rung`, `date` |
| `fallback_price` | запасная ступень вне ядра по другой причине (нет цены, условия, которые ядро не берёт): простой PnL | `rung`, `why`, `date` |
| `yield_no_curve` | валютная бумага без кривой в валюте: кэрри по доходности, изменение доходности в `other` (`desk-flash-yield-v1`) | `ccy` |
| `reported_value` | разница отчётных стоимостей (`desk-reported-v1`) | `why` |
| `no_terms` | то же, но оценка по отчёту из-за отсутствия условий: бумаги нет в справочнике, у сделки нет `trade_terms` | — |
| `fx_core_rest` | FX-сделка ядром: всё, что не FX-дельта (`desk-fx-core-v1`) | — |

Ступень, которую ядро оценило по её цене (`market_value`, `stale_price` — TDM#383, `model_price` — TDM#441), на обеих
датах идёт точными шагами (`fallback.by_core`); модельная цена ядром даёт тот же Z, из которого построена
(`MODEL_Z_TOLERANCE_BP`), поэтому перенесённый спред не дрейфует.

## Что сохраняет flash (TDM#406)

Сохранение в flash (TDM#406: `backend/src/desk/pnl/flash.py`, определения — в docstring модуля). Только методы
`desk-flash-exact-*`; прямые и простые методы и прогоны до TDM#406 ключей не имеют (→ `null`).

- `carry_coupon` — купонный доход за (D-1, D] по графику D-1: `q × (НКД(D) − НКД(D-1) + купоны, выплаченные внутри)`,
  НКД на бумагу по инструменту ядра на датах оценки (флоатер — по фиксингам D-1); у валютной бумаги в её валюте ×
  курс D, как `carry`. `carry_pull = carry − carry_coupon`. Также `received_coupon_rub` + `received_principal_rub` =
  `received_rub`.
- `rates_linear_curves: {кривая: {срок: руб}}` (кривые — как в `fact.risk`), сумма = `rates_linear`; срок без сдвига
  не пишется. `curve_moves_bp: {кривая: {срок: б.п.}}` — использованные сдвиги. У ОФЗ в RUONIA + базис — только
  `RUB OFZ`; `rates_linear_ruonia`/`rates_linear_basis` делят тот же итог по факторам, а не по срокам.
- `prev: {clean_pct, accrued_per_bond_rub, dirty_per_bond_rub, ytm_pct, pv_rub, dv01_rub, cs01_rub, ofz_spr01_rub,
  quantity, risk, ofz_ruonia_risk?}` — оценка D-1, от которой идут шаги, при количестве D. В копию PnL в `fact.extra.pnl`
  эти ключи не попадают.
