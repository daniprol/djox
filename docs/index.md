# djox

A truly async Django ORM with a Rust core. Fast and readable, with hooks for
library authors. SQLite and Postgres.

!!! warning "Status: early"
    djox has its engine (`djox.Database` runs SQL from async code on
    connections that Rust owns), a Django `ENGINE` that puts Django's own
    queries on the same SQLite, and `djox.Manager`, which runs Django's async
    QuerySet methods (`await Book.objects.aget(...)`) on djox. Async
    transactions come next; see the [roadmap](roadmap.md).

## Why

Django's async ORM methods (`aget`, `acreate`, `async for`...) run the sync
ORM on a worker thread and wait for it. On SQLite that hop costs more than the
query itself, and every query of a request waits for the same thread. djox
runs queries on its own connections, each on a Rust thread, and wakes Python
only when the answer is ready.

## Install

```bash
uv add djox  # or: pip install djox  (not on PyPI yet: build from source with `uv sync`)
```

## Quickstart

```python
--8 < --"docs/examples/quickstart.py"
```

- SQL uses Django's `%s` placeholders (`%%` for a literal `%`).
- The query methods send the statement right away and return an
  `asyncio.Future`: await it, or pass it to `asyncio.gather()`.
- Rows are tuples of `None`, `int`, `float`, `str` and `bytes`, like Python's
  `sqlite3`. Parameters can also be `bool`, `datetime`, `date`, `time`,
  `Decimal` and `UUID`, stored the way Django's SQLite backend stores them.
- Errors follow PEP 249, like Python's `sqlite3`: `djox.IntegrityError` (a
  constraint failed), `djox.OperationalError` (SQLite couldn't run the
  statement), `djox.ProgrammingError` (a wrong number of parameters...), all
  subclasses of `djox.DatabaseError`; `djox.DatabaseClosedError` after
  `close()`.

!!! danger "Using djox next to Django? Use djox's `ENGINE`"
    djox bundles its own copy of SQLite, and Python's `sqlite3` module has
    another. Two copies in one process can't see each other's locks, so
    writes can be lost if both open the same file. Set
    `"ENGINE": "djox.backends.sqlite3"` and Django uses djox's copy too: see
    [Use with Django](django.md).
