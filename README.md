# djox

**Truly async Django ORM with a Rust core 🦀** Fast and readable, with hooks for library authors. SQLite and Postgres.

Django's async ORM methods (`aget`, `acreate`, `async for`...) run the sync ORM on a worker thread and wait for it.
djox runs queries on connections that Rust owns, each on its own thread, and wakes Python only when the answer is
ready. Django keeps doing what it does well: models, migrations, admin and the QuerySet language.

> **Status: early.** djox has its engine, `djox.Database`, a Django `ENGINE` that runs Django's own queries on the
> same SQLite, `djox.Manager` (Django's QuerySet methods on djox, with a fast lane for simple queries, sync and
> async), async transactions (`djox.atomic()`), hooks, and a Postgres backend. See the [roadmap](docs/roadmap.md).

## Quickstart

```python
import asyncio

import djox


async def main() -> None:
    with djox.Database.sqlite("app.sqlite3") as database:
        await database.execute("CREATE TABLE IF NOT EXISTS book (id INTEGER PRIMARY KEY, title TEXT, pages INTEGER)")
        await database.execute_many(
            "INSERT INTO book (title, pages) VALUES (%s, %s)",
            [["Dune", 412], ["Emma", 474], ["Beloved", 324]],
        )
        rows = await database.fetch_all("SELECT title FROM book WHERE pages > %s ORDER BY title", [400])
        print(rows)  # [('Dune',), ('Emma',)]


asyncio.run(main())
```

- **One writer, a few readers.** SQLite allows one writer at a time, so djox opens one writer connection and queues
  every write for it: no "database is locked" errors. Reads run in parallel on read-only connections, in WAL mode.
- **Django's SQL as is.** `%s` placeholders, values stored the way Django's SQLite backend stores them, and SQL from
  Django's own compiler runs unchanged (including `select_related` joins).
- **Never blocks the event loop.** A slow query keeps running on its thread while your other tasks go on.

## With Django

```python
# settings.py
DATABASES = {"default": {"ENGINE": "djox.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
```

Django's ORM, migrations, admin and tests keep working, on the SQLite bundled in djox. Give a model
`objects = djox.Manager()` and its async methods (`aget`, `acount`, `async for`, `acreate`, `aupdate`...) run on djox
instead of Django's worker thread: Django still writes the SQL and builds the objects. `djox.connections["default"]`
gives the async `djox.Database` for the same file, `async with djox.atomic():` is Django's `atomic()` for async
code, and `djox.Model` runs `asave()` and friends on djox's threads. For models you can't edit (`User`, other
libraries'), add `"djox"` to `INSTALLED_APPS`: `DJOX_MODELS` picks which models switch to djox.
`djox.add_hooks(djox.Hooks(before_query=..., after_query=...))` calls your functions around every statement, sync or
async. One SQLite in the process matters: Python's `sqlite3` module has its own copy, and two copies can't see each
other's locks. See [Use with Django](docs/django.md).

## Speed

Microseconds per call on a 4-core cloud VM, lower is better ([method and all numbers](benchmarks/README.md)):

| | Django sync | `djox.Manager` sync | Django async | `djox.Manager` async | djox (raw SQL) |
|---|---:|---:|---:|---:|---:|
| get one book by id | 187 | 58 | 605 | 214 | 89 |
| fetch 1,000 books | 8,478 | 4,693 | 12,824 | 4,667 | 1,187 |
| count | 293 | 87 | 775 | 237 | 142 |
| update one book | 121 | 54 | 495 | 215 | 108 |
| 100 gets at once (each) | | | 559 | 186 | 77 |

`djox.Manager` runs the SQL on djox, so no async query waits for Django's single worker thread. Simple queries, sync
or async, also take the fast lane: they reuse the SQL Django wrote for the same shape of query, and Rust builds the
results. A sync `get()` is then three times faster than Django's, and an async `aget()` on djox is about as fast as
Django's own sync `get()`.

## How it is built

| Path                | What it is                                                          |
|---------------------|---------------------------------------------------------------------|
| `crates/djox-core`  | The shared types and the `Database` trait every backend implements  |
| `crates/djox`       | The backends, one folder each: `sqlite/` and `postgres/`             |
| `src/*.rs`          | The Python extension `djox._core` (PyO3)                             |
| `src/djox`          | The Python package, and Django's `ENGINE` (`djox.backends.sqlite3`)  |

The Rust code is written for Python developers who have never seen Rust: plain names, straight-line code, and
comments that explain each Rust feature (and each database detail) where it appears. See
[How it works](docs/how-it-works.md) and [AGENTS.md](AGENTS.md).

## Develop

```bash
uv sync                              # builds the Rust extension
uv run pytest                        # Python tests
scripts/cargo-test.sh --workspace    # Rust tests
uv run prek install                  # git hooks: ruff, mypy, pyrefly, cargo fmt, clippy, tests
```

## License

MIT
