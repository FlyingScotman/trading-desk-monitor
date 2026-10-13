# Trading Desk Monitor

Монитор FI-деска: риски по кривым (key-rate DV01), кредитный риск, ОВП, фондирование, сценарии и PnL explain. Четыре экрана в браузере, общая база расчётов, без внешних зависимостей (Python 3.9+, стандартная библиотека).

```
расчётный сервис ──► SQLite (data/tdm.sqlite) ──► python -m tdm serve ──► браузеры
 (in-house либа)       прогоны: loading → complete       :8080
```

| Экран | Для чего |
|---|---|
| **Desk Monitor** | сводка, позиции с карточкой, риски по кривым, экспозиции, фондирование, сценарии, PnL |
| **Widget Board** | свои доски из виджетов (утро / rates / credit / FX) |
| **Pivot Studio** | сводные таблицы и тепловые карты по любым разрезам |
| **Scenario & PnL Lab** | конструктор сценариев, разбор PnL explain |

Лимиты и VaR сюда не входят: их считают отдельные модули.

## Быстрый старт

```bash
python -m tdm demo                 # создать базу и загрузить демо-прогон
python -m tdm serve --port 8080    # открыть http://localhost:8080/
python -m unittest discover -s tests
```

На Windows — `install.bat`, затем `run.bat`. Подробно: [docs/INSTALL_WINDOWS.md](docs/INSTALL_WINDOWS.md).

## Данные

Что считать, куда класть и как это отдаётся — [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md).
Загрузка: Python-SDK `tdm.writer.Run` (шаблон — `examples/calc_service_stub.py`) или JSON-пакет (`contract/bundle.schema.json`, пример — `fixtures/demo_bundle.json`) через `python -m tdm import`.

Страницы можно открыть и просто как файлы (`prototypes/*.html`): тогда они работают на демо-наборе `prototypes/data.js`.

## Структура

```
prototypes/   страницы UI, desk-loader.js (откуда брать данные), desk-util.js (расчёты в браузере), data.js (демо)
tdm/          schema.sql, writer.py (SDK), importer.py, assemble.py (БД → window.DESK), server.py, cli.py
contract/     JSON Schema пакета прогона
fixtures/     демо-пакет (выгружен из data.js: tools/export_demo_bundle.js)
examples/     шаблон расчётного сервиса
docs/         контракт данных, установка на Windows
tests/        тесты (unittest)
```

Прототипы перенесены из `Balance-sheet-playground` (ветка `claude/zen-ritchie-ao73d8`).
