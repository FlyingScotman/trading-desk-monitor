SPI_VERSION = 2


def describe():
    return {"name": "wrong_spi", "version": "0.0.1", "spi": 2, "contract": "private/1.0", "sources": []}


def match(path, ctx):
    return None


def read(path, hit, ctx):
    return {}
