# How it works

## The journey of a query

An ORM turns method calls into SQL, runs the SQL, and turns rows back into
objects. Django does all three in Python, and only the middle step waits for
the database. djox keeps Django for the first and last steps and replaces the
middle one:

1. **Django builds the SQL**: `Book.objects.filter(pages__gt=100)` becomes
   `("SELECT ... WHERE pages > %s", [100])`. This is pure Python and never
   touches the database.
2. **djox runs it**: the statement goes into a queue; a Rust thread that owns
   a SQLite connection runs it and converts the rows.
3. **Django builds the objects** with its own converters and `Model.from_db()`.

## The SQLite backend

SQLite is a library, not a server: every call blocks until it is done. So
each connection lives on its own thread, a *worker*, which takes statements
from a queue one at a time. Python puts a statement in a queue and awaits a
Future; the worker completes the Future through `loop.call_soon_threadsafe`.
The event loop never waits.

```text
  Python (asyncio)            Rust
  ──────────────────          ───────────────────────────────────────────
  await execute(...)   ──►   writer queue  ──►  writer thread  ──┐
  await fetch_all(...) ──►   reader queue  ──►  reader thread 1 ─┼──► app.sqlite3
                                           ──►  reader thread 2 ─┘
```

- **One writer.** SQLite allows one writer at a time for the whole file. djox
  opens exactly one writer connection and queues every write for it, so
  writes never fail with "database is locked".
- **A few readers** (one per CPU core, at most 4) share another queue and run
  in parallel. They are opened read-only; a statement sent to `fetch_all`
  that turns out to write (`INSERT ... RETURNING`) is passed to the writer.
- **WAL mode**: readers never wait for the writer, and the writer never waits
  for readers.
- **`synchronous = NORMAL`**: safe with WAL and much faster than Django's
  default; a power cut can lose the last commits, never corrupt the file.

## The code

| Path                | What it is                                                          |
|---------------------|---------------------------------------------------------------------|
| `crates/djox-core`  | The shared types and the `Database` trait every backend implements  |
| `crates/djox`       | The backends, one folder each: `sqlite/` today, `postgres/` later    |
| `src/*.rs`          | The Python extension `djox._core`                                    |
| `src/djox`          | The Python package                                                   |

Each backend owns everything about its database. Postgres will be a new
folder with its own connection pool, behind the same `Database` trait, and
`djox.Database.postgres(...)` next to `djox.Database.sqlite(...)`.

The Rust code is written for Python developers who have never seen Rust:
plain names, straight-line code, and comments that explain each Rust feature
the first time it appears.
