#!/usr/bin/env python3
"""Генерирует из tdm/datasets.py:
  contract/csv/<набор>.csv            — шаблон: заголовок + строка-пример (разделитель «;», UTF-8 с BOM для Excel)
  docs/DATA_REQUIREMENTS.md           — раздел между маркерами <!-- datasets:begin --> и <!-- datasets:end -->

    python tools/gen_data_docs.py
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tdm.datasets import DATASETS  # noqa: E402

PRIO = {1: "обязательно", 2: "желательно", 3: "если есть"}
KIND = {"ref": "справочник (актуальная версия, upsert по ключу)",
        "snapshot": "срез на дату (файл заменяет свои даты{by})",
        "series": "временной ряд (upsert по дате и ключу)"}
TYPE = {"str": "текст", "num": "число", "int": "целое", "date": "дата", "bool": "1/0"}


def write_templates():
    out = ROOT / "contract" / "csv"
    out.mkdir(parents=True, exist_ok=True)
    for d in DATASETS:
        with open(out / f"{d.name}.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow([c.name for c in d.cols])
            w.writerow([c.example for c in d.cols])


def render():
    lines = []
    lines.append("| # | Набор (файл) | Что | Приоритет | Глубина | Источник |")
    lines.append("|---|---|---|---|---|---|")
    for i, d in enumerate(DATASETS, 1):
        lines.append(f"| {i} | [`{d.name}`](#{d.name}) | {d.title} | {PRIO[d.priority]} | {d.depth} | {d.sources} |")
    for i, d in enumerate(DATASETS, 1):
        by = (", книги" if "book_id" in d.replace_by else "") if d.kind == "snapshot" else ""
        lines += ["", f'<a id="{d.name}"></a>', f"### {i}. {d.title} — `{d.name}.csv`", ""]
        lines.append(f"- **Зачем:** {d.purpose}")
        lines.append(f"- **Приоритет:** {PRIO[d.priority]}. **Глубина:** {d.depth}.")
        lines.append(f"- **Где брать:** {d.sources}.")
        how = "справочник (файл заменяет его целиком)" if d.replace_all else KIND[d.kind].format(by=by)
        lines.append(f"- **Как грузится:** {how}; ключ `{', '.join(d.key)}` → таблица `{d.table}`. "
                     f"Шаблон: `contract/csv/{d.name}.csv`.")
        lines += ["", "| Поле | Тип | Обяз. | Описание | Пример |", "|---|---|:-:|---|---|"]
        for c in d.cols:
            lines.append(f"| `{c.name}` | {TYPE[c.type]} | {'●' if c.required else ''} | {c.desc} | {c.example} |")
    return "\n".join(lines)


def write_doc():
    p = ROOT / "docs" / "DATA_REQUIREMENTS.md"
    s = p.read_text(encoding="utf-8")
    a, b = "<!-- datasets:begin -->", "<!-- datasets:end -->"
    i, j = s.index(a) + len(a), s.index(b)
    p.write_text(s[:i] + "\n" + render() + "\n" + s[j:], encoding="utf-8")


if __name__ == "__main__":
    write_templates()
    write_doc()
    print("contract/csv/*.csv и docs/DATA_REQUIREMENTS.md обновлены")
