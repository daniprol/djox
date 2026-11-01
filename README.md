# djox

**Truly async Django ORM with a Rust core 🦀** Fast and readable, with hooks for library authors. SQLite first.

Django's async ORM methods (`aget`, `acreate`, `async for`...) run the sync ORM on a worker thread and wait for it.
djox runs queries on connections that Rust owns, each on its own thread, and wakes Python only when the answer is
ready. Django keeps doing what it does well: models, migrations, admin and the QuerySet language.

> **Status: early.** This first release is the engine, `djox.Database`. The Django-style API
> (`await Book.objects.aget(...)` running on djox) comes next. See the [roadmap](docs/roadmap.md).

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

## Speed

Microseconds per call on a 4-core cloud VM, lower is better ([method and all numbers](benchmarks/README.md)):

| | Django sync | Django async | djox |
|---|---:|---:|---:|
| get one book by id | 378 | 854 | 207 |
| fetch 20 books | 838 | 1,437 | 299 |
| 100 gets at once (each) | | 686 | 144 |

djox's numbers are raw SQL; Django's include building the query and the model instances. Those Python costs are what
the next steps of the roadmap take on.

## How it is built

| Path                | What it is                                                          |
|---------------------|---------------------------------------------------------------------|
| `crates/djox-core`  | The shared types and the `Database` trait every backend implements  |
| `crates/djox`       | The backends, one folder each: `sqlite/` today, `postgres/` later    |
| `src/*.rs`          | The Python extension `djox._core` (PyO3)                             |
| `src/djox`          | The Python package                                                   |

The Rust code is written for Python developers who have never seen Rust: plain names, straight-line code, and
comments that explain each Rust feature (and each database detail) where it appears. See
[How it works](docs/how-it-works.md) and [AGENTS.md](AGENTS.md).

## Develop

```bash
uv sync                              # builds the Rust extension
uv run pytest                        # Python tests
scripts/cargo-test.sh --workspace    # Rust tests
uv run prek install                  # git hooks: ruff, ty, cargo fmt, clippy, tests
```

## License

MIT
