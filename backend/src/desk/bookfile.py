"""A book file (``position_id,book_id,secid,quantity_bonds`` CSV, ``#`` lines are notes) into the desk store: ``desk
load-positions`` and ``desk eod --book`` (it lived in ``desk.cli``, TDM#644)."""

from __future__ import annotations

import csv
import getpass
import hashlib
from collections.abc import Callable
from datetime import date
from pathlib import Path

import psycopg

from . import store


def read_book(path: Path) -> list[tuple[str, str, str, float]]:
    with path.open(encoding="utf-8") as f:
        return [(r["position_id"], r["book_id"], r["secid"], float(r["quantity_bonds"]))
                for r in csv.DictReader(f) if not r["position_id"].startswith("#")]


def load_book(con: psycopg.Connection, path: Path, d: str, books: list[str] | None = None,
          out: Callable[[str], None] = print) -> None:
    rows = read_book(path)
    loaded = store.load_positions(con, rows, valid_date=date.fromisoformat(d), source_name=path.name,
                                  source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                  recorded_by=getpass.getuser(), books=books)
    out(f"positions {path.name} for {d}: " + ", ".join(
        f"{x.book_id} rev {x.revision}" + ("" if x.changed else " unchanged") for x in loaded))
