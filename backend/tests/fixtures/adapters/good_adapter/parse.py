"""CSV parsing of the test adapter, in a submodule so the package's own relative imports are exercised."""

import csv
import os
import re

NAME = re.compile(r"^positions_([0-9]{4})([0-9]{2})([0-9]{2})(?:_([0-9]+))?\.csv$")


def records(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader, [])
        for values in reader:
            rec = {k: v for k, v in zip(header, values) if v}
            if rec:
                yield reader.line_num, rec


def hit(path):
    m = NAME.match(os.path.basename(path))
    if not m:
        return None
    return {"source": "positions_daily", "report_date": f"{m[1]}-{m[2]}-{m[3]}", "date_from": "name",
            "report_ts": None, "seq": int(m[4]) if m[4] else None}
