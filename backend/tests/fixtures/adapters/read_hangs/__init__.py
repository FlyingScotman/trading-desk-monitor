import time

SPI_VERSION = 1


def describe():
    return {"name": "read_hangs", "version": "0.0.1", "spi": 1, "contract": "private/1.0",
            "sources": [{"source": "market", "dataset": "internal_market", "time_basis": "end_of_day",
                         "completeness": "full_snapshot", "scope": ["kind"], "covers": "listed"}]}


def match(path, ctx):
    return {"source": "market", "report_date": "2026-10-15", "date_from": "name", "report_ts": None, "seq": None}


def read(path, hit, ctx):
    time.sleep(60)
    return {}
