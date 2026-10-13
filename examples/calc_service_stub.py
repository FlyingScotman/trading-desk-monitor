"""Шаблон расчётного сервиса: как выложить результаты в Trading Desk Monitor.

Замените функции load_* на вызовы вашей расчётной библиотеки / статики / рыночных данных.
Сейчас они берут данные из демо-пакета, чтобы шаблон запускался как есть:

    python examples/calc_service_stub.py --db data/tdm.sqlite --as-of 2026-10-13

Подключение пакета tdm в окружении расчётного сервиса (без интернета):
    вариант 1: sys.path.insert(0, r"C:\\tdm")             # путь к папке с этим репозиторием
    вариант 2: pip install --no-build-isolation C:\\tdm
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tdm.writer import Run, upsert_scenarios  # noqa: E402

DEMO = json.loads((ROOT / "fixtures" / "demo_bundle.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- статика
def load_books():
    """[{book_id, name, desk, trader}] — из вашей системы учёта книг."""
    return DEMO["books"]


def load_issuers():
    """[{issuer, issuer_group, sector, rating, is_gov, is_quasi}] — справочник эмитентов."""
    return DEMO["issuers"]


# ---------------------------------------------------------------- рынок
def load_market(as_of):
    """Кривые (по всем 11 бакетам: уровень % и изменение за день б.п.), курсы, индексы."""
    return DEMO["curves"], DEMO["fx"], DEMO["indices"]


# ---------------------------------------------------------------- расчёт
def calc_positions(as_of, curves, fx):
    """Для каждой позиции: атрибуты, PV, DV01/CS01, key-rate DV01 (risk), FX-дельта и PnL explain.
    Генератор/список словарей; можно отдавать порциями по несколько тысяч."""
    for p in DEMO["positions"]:
        yield p


def calc_funding(as_of, fx):
    return DEMO["funding"]


def calc_scenarios(as_of):
    """Необязательно: полная переоценка пресетов. {scenario_id: (positions, funding_residual)}"""
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "tdm.sqlite"))
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--run-type", default="EOD", choices=["EOD", "INTRADAY", "TEST"])
    a = ap.parse_args()

    upsert_scenarios(a.db, DEMO["scenarios"])  # пресеты сценариев: при изменении списка
    curves, fx, indices = load_market(a.as_of)
    with Run(a.db, as_of=a.as_of, run_type=a.run_type, source="calc_service_stub", lib_version="demo") as run:
        run.add_books(load_books())
        run.add_issuers(load_issuers())
        run.add_curves(curves)
        run.add_fx(fx)
        run.add_indices(indices)
        batch = []
        for p in calc_positions(a.as_of, curves, fx):
            batch.append(p)
            if len(batch) >= 5000:
                run.add_positions(batch)
                batch = []
        run.add_positions(batch)
        run.add_funding(calc_funding(a.as_of, fx))
        for sid, (pos, resid) in calc_scenarios(a.as_of).items():
            run.add_scenario_results(sid, pos, resid)
        run.log("INFO", "расчёт завершён")
    print(f"прогон {run.run_id}: {run.status}; предупреждений {len(run.warnings)}")


if __name__ == "__main__":
    main()
