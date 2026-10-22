# Trading Desk Monitor

Монитор FI-деска: риски по кривым (key-rate DV01), кредитный риск, ОВП, фондирование, сценарии и PnL explain. Четыре экрана в браузере, общая база расчётов, без внешних зависимостей (Python 3.9+, стандартная библиотека).

```
расчётный сервис ──► SQLite (data/tdm.sqlite) ──► python -m tdm serve ──► браузеры
 (in-house либа)       прогоны: loading → complete       :8080
```

| Экран | Для чего |
|---|---|
| **Desk Monitor** | сводка, позиции с карточкой, **облигации** (доходность, спред к выбранной кривой, дюрация, позиция, риск; карта доходностей; история цены/доходности/спреда выпуска за год), риски по кривым, экспозиции, фондирование, сценарии, PnL |
| **Widget Board** | свои доски из виджетов (утро / rates / credit / FX), в т.ч. «Облигации» и «История выпуска» |
| **Pivot Studio** | сводные таблицы и тепловые карты по любым разрезам |
| **Scenario & PnL Lab** | конструктор сценариев, разбор PnL explain (флэш → курсы ЦБ → официальный), **кэрри и фондирование на горизонте** при траектории КС |

Лимиты и VaR сюда не входят: их считают отдельные модули.

Страницы связаны: строка навигации вверху каждой (и React-приложения `/app/`) ведёт на любую страницу и несёт общий
выбор прогона (дата/номер — во всех страницах один), показывает автопилот с кнопкой «Обновить» и плашку «Тестовые данные»,
только когда страница и правда на демо-наборе. Фильтр Desk Monitor (юрлицо ∩ деск ∩ портфель ∩ книги ∩ сегмент ∩ валюта)
действует и на Widget Board, Pivot Studio и Scenario Lab; строка показывает его и снимает.

## Быстрый старт

```bash
python -m tdm demo                 # создать базу, загрузить демо-прогон и историю рынка за год
python -m tdm serve --port 8080    # открыть http://localhost:8080/
python -m unittest discover -s tests
```

На Windows — `install.bat`, затем `run.bat`. Подробно: [docs/INSTALL_WINDOWS.md](docs/INSTALL_WINDOWS.md).

## Центральный сервис данных

`tdm` хранит входные данные (справочники, ежедневные срезы позиций и фондирования, рыночные ряды, официальный PnL), результаты расчётов (риски, PnL explain, полная переоценка сценариев) и производные (пилларсы и изменения кривых, спреды по рейтингам, исторические сценарии) и отдаёт их по HTTP (`/api/v1/*`).

| Документ | О чём |
|---|---|
| [docs/architecture/](docs/architecture/README.md) | **архитектура системы** (с 16.10.2026): компоненты, контракт рыночных данных ↔ core, разделение расчётов бэкенд/фронтенд, бэкенд, интеграция фронтенда, развёртывание, план развития core, roadmap, ADR |
| [docs/SERVICE_DATA_MODEL.md](docs/SERVICE_DATA_MODEL.md) | что сервис хранит и отдаёт: слои, сущности, ключи, API, какие данные нужны каждому экрану |
| [docs/DATA_REQUIREMENTS.md](docs/DATA_REQUIREMENTS.md) | **список данных к сбору** (23 набора, поля, источники, приоритеты) + шаблоны `contract/csv/` |
| [docs/SCENARIOS.md](docs/SCENARIOS.md) | сценарии: греки в мониторе / полная переоценка заранее / по запросу; исторические сценарии |
| [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md) | как расчётный сервис выкладывает результаты (прогоны) |
| [docs/ORG_CONVENTIONS.md](docs/ORG_CONVENTIONS.md) | **общая методика** фондирования, FX, PnL, кривых и сценариев (контракт 1.3, `DATA_CONTRACT.md` §0). Правила конкретной организации — в приватном адаптере (ADR-008) |

```bash
python -m tdm load C:\history\                 # загрузить CSV (справочники, срезы, рынок)
python -m tdm coverage                          # что загружено на какие даты
python -m tdm build-hist-scenarios              # исторические сценарии из рядов
python -m tdm attach-official --as-of 2026-10-13   # T+1: официальный PnL (pnl_official) → опубликованный EOD-прогон
```

## Результаты расчётов

Что считать, куда класть и как это отдаётся — [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md).
Загрузка: Python-SDK `tdm.writer.Run` (шаблон — `examples/calc_service_stub.py`) или JSON-пакет (`contract/bundle.schema.json`, пример — `fixtures/demo_bundle.json`, контракт 1.3) через `python -m tdm import`.
Фондирование, которого расчётный сервис не выложил, строит `tdm`: `run.derive_funding()` (`DATA_CONTRACT.md` §0.9).

Страницы можно открыть и просто как файлы (`prototypes/*.html`): тогда они работают на демо-наборе `prototypes/data.js`.

## Структура

```
prototypes/   страницы UI, desk-nav.js (строка навигации, общий прогон, автопилот), desk-loader.js (откуда брать данные),
              desk-util.js (расчёты в браузере), desk-scope.js (фильтр Desk Monitor на других страницах), data.js (демо)
tdm/          schema.sql, datasets.py (спецификация входных наборов), loader.py (CSV), market.py, histscen.py,
              writer.py (SDK), importer.py, assemble.py (БД → window.DESK), api.py, server.py, cli.py
contract/     JSON Schema пакета прогона, csv/ — шаблоны входных наборов
fixtures/     демо-пакет и история рынка за год (выгружены из data.js: tools/export_demo_bundle.js)
examples/     шаблон расчётного сервиса
docs/         контракт данных, установка на Windows
tests/        тесты (unittest)
```

Прототипы перенесены из `Balance-sheet-playground` (ветка `claude/zen-ritchie-ao73d8`).
