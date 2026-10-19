SPI_VERSION = 1


def describe():
    return {"name": "match_raises", "version": "0.0.1", "spi": 1, "contract": "private/1.0",
            "sources": [{"source": "pnl", "dataset": "internal_market", "time_basis": "end_of_day",
                         "completeness": "full_snapshot", "scope": ["kind"], "covers": "listed"}]}


def match(path, ctx):
    if path.endswith(".csv"):
        raise ValueError("cell value 4242.42 must never leave the child")


def read(path, hit, ctx):
    return {}
