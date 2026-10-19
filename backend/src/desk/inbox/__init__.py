"""The inbox: bank reports read through a private adapter (SPI v1, docs/architecture/08_PRIVATE_ADAPTER_INTERFACE.md).

Every module here imports only the standard library and ``desk.inbox``, so the adapter's own tests can run the
conformance kit from a sibling checkout without psycopg, core or FastAPI (08 §8). The exception is ``scan``, the
store side (the ledger and ``desk inbox``), which nothing of the kit imports.
"""
