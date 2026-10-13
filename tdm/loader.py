"""Загрузка входных данных из CSV (шаблоны — contract/csv/, описание — docs/DATA_REQUIREMENTS.md).

    python -m tdm load C:\\history\\                 # все *.csv в папке (и подпапках)
    python -m tdm load md_fx_2025.csv positions_2026-10-13.csv

Набор определяется по имени файла: <набор>.csv или <набор>_<любой суффикс>.csv.
Разделитель «;» или «,»; при «;» допускается десятичная запятая (выгрузка из Excel).
Кодировка UTF-8 (с BOM или без) или cp1251. Один файл = одна транзакция: при любой ошибке
в строке файл не загружается целиком, в отчёте — номера строк.
"""
import csv
import io
import re
from datetime import date
from pathlib import Path

from .datasets import BY_NAME, DATASETS
from .db import init_db, now_iso

_TRUE = {"1", "true", "yes", "y", "да", "д", "t"}
_FALSE = {"0", "false", "no", "n", "нет", "н", "f", ""}


class LoadError(Exception):
    pass


def dataset_for(path):
    stem = Path(path).stem.lower()
    for d in sorted(DATASETS, key=lambda d: -len(d.name)):  # issuer_ratings раньше issuers
        if stem == d.name or stem.startswith(d.name + "_") or stem.startswith(d.name + "-"):
            return d
    return None


def _read_text(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise LoadError("не удалось определить кодировку (ожидается UTF-8 или cp1251)")


def _parse(value, typ, decimal_comma):
    v = (value or "").strip()
    if v == "":
        return None
    if typ == "str":
        return v
    if typ in ("num", "int"):
        s = v.replace("\u00a0", "").replace(" ", "")
        if decimal_comma:
            s = s.replace(",", ".")
        x = float(s)
        if typ == "int":
            if x != int(x):
                raise ValueError(f"ожидалось целое: {v}")
            return int(x)
        return x
    if typ == "date":
        if re.match(r"^\d{2}\.\d{2}\.\d{4}$", v):          # 13.10.2026 из Excel
            v = f"{v[6:10]}-{v[3:5]}-{v[0:2]}"
        return date.fromisoformat(v[:10]).isoformat()
    if typ == "bool":
        low = v.lower()
        if low in _TRUE:
            return 1
        if low in _FALSE:
            return 0
        raise ValueError(f"ожидалось 1/0: {v}")
    raise ValueError(f"неизвестный тип {typ}")


def read_rows(ds, path):
    """Прочитать и проверить файл. Возвращает (rows, errors, warnings)."""
    text = _read_text(path)
    first = text.split("\n", 1)[0]
    delim = ";" if first.count(";") > first.count(",") else ("\t" if "\t" in first else ",")
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    header = [h.strip() for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    known = {c.name for c in ds.cols}
    errors, warnings = [], []
    missing = [c.name for c in ds.cols if c.required and c.name not in header]
    if missing:
        return [], [f"нет обязательных колонок: {', '.join(missing)}"], []
    unknown = [h for h in header if h and h not in known]
    if unknown:
        warnings.append(f"неизвестные колонки (будут проигнорированы): {', '.join(unknown)}")
    rows, seen = [], {}
    for n, rec in enumerate(reader, start=2):
        if not any((v or "").strip() for v in rec.values() if isinstance(v, str)):
            continue
        row, bad = {}, []
        for c in ds.cols:
            try:
                row[c.name] = _parse(rec.get(c.name), c.type, delim == ";")
            except ValueError as e:
                bad.append(f"{c.name}: {e}")
                continue
            if c.required and row[c.name] is None:
                bad.append(f"{c.name}: пусто")
        if ds.name == "pnl_official" and row.get("position_id") is None:
            row["position_id"] = ""
        if ds.name == "positions" and not row.get("instrument_id") and not row.get("deal_id"):
            bad.append("нужен instrument_id или deal_id")
        if ds.name == "positions" and row.get("quantity") is None and row.get("face_amount") is None:
            bad.append("нужен quantity или face_amount")
        k = tuple(row.get(x) for x in ds.key)
        if not bad and k in seen:
            bad.append(f"дубликат ключа {k} (строка {seen[k]})")
        seen.setdefault(k, n)
        if bad:
            errors.append(f"строка {n}: " + "; ".join(bad))
        else:
            rows.append(row)
    return rows, errors, warnings


def load_file(db_path, path, dataset=None, con=None):
    """Загрузить один файл. Возвращает dict-отчёт; при ошибках данные не меняются."""
    ds = BY_NAME[dataset] if dataset else dataset_for(path)
    if ds is None:
        raise LoadError(f"{Path(path).name}: не понимаю, какой это набор (имя файла должно начинаться с одного из: "
                        + ", ".join(d.name for d in DATASETS) + ")")
    own = con is None
    con = con or init_db(db_path)
    try:
        rows, errors, warnings = read_rows(ds, path)
        dates = sorted({r[ds.date_col] for r in rows}) if ds.date_col else []
        report = {"file": str(path), "dataset": ds.name, "rows": len(rows), "errors": errors, "warnings": warnings,
                  "date_from": dates[0] if dates else None, "date_to": dates[-1] if dates else None}
        if errors:
            _log(con, ds, path, report, "error")
            return report
        cols = [c.name for c in ds.cols]
        ts = now_iso()
        with con:
            if ds.kind == "snapshot":
                groups = {tuple(r[c] for c in ds.replace_by) for r in rows}
                for g in groups:
                    con.execute(f"DELETE FROM {ds.table} WHERE " + " AND ".join(f"{c}=?" for c in ds.replace_by), g)
            if ds.name == "desk_history":
                sql = ("INSERT INTO desk_history (as_of, run_id, pnl_official, pnl_explained, dv01, cs01, updated_at) VALUES (?,NULL,?,?,?,?,?)"
                       " ON CONFLICT(as_of) DO UPDATE SET pnl_official=excluded.pnl_official, pnl_explained=excluded.pnl_explained,"
                       " dv01=excluded.dv01, cs01=excluded.cs01, updated_at=excluded.updated_at WHERE desk_history.run_id IS NULL")
                con.executemany(sql, [(r["as_of"], r["pnl_official"], r["pnl_explained"], r["dv01"], r["cs01"], ts) for r in rows])
            else:
                upd = [c for c in cols if c not in ds.key] + ["updated_at"]
                sql = (f"INSERT INTO {ds.table} ({', '.join(cols)}, updated_at) VALUES ({', '.join('?' * (len(cols) + 1))})"
                       f" ON CONFLICT({', '.join(ds.key)}) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in upd))
                con.executemany(sql, [[r[c] for c in cols] + [ts] for r in rows])
        _log(con, ds, path, report, "ok")
        return report
    finally:
        if own:
            con.close()


def _log(con, ds, path, report, status):
    with con:
        con.execute("INSERT INTO data_load (dataset, file_name, loaded_at, rows, date_from, date_to, status, error) VALUES (?,?,?,?,?,?,?,?)",
                    (ds.name, Path(path).name, now_iso(), report["rows"], report["date_from"], report["date_to"], status,
                     "\n".join(report["errors"][:200]) or None))


def load_paths(db_path, paths):
    """Файлы и папки. Справочники грузятся раньше срезов и рядов. Возвращает список отчётов."""
    files = []
    for p in paths:
        p = Path(p)
        files += sorted(p.rglob("*.csv")) if p.is_dir() else [p]
    order = {d.name: i for i, d in enumerate(DATASETS)}
    files.sort(key=lambda f: (order.get(dataset_for(f).name, 999) if dataset_for(f) else 999, str(f)))
    con = init_db(db_path)
    out = []
    try:
        for f in files:
            try:
                out.append(load_file(db_path, f, con=con))
            except LoadError as e:
                out.append({"file": str(f), "dataset": None, "rows": 0, "errors": [str(e)], "warnings": []})
    finally:
        con.close()
    return out
