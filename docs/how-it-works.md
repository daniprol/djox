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

## Django's own queries

Django's sync ORM (and everything built on it: migrations, the admin, the
test runner) talks to SQLite through a `DatabaseWrapper`, one per thread,
which holds a connection shaped like Python's `sqlite3.Connection`. djox's
`ENGINE` (`djox.backends.sqlite3`) is Django's own SQLite backend with one
change: the connection is a `djox.Connection`, which runs on djox's SQLite.

```text
  Django sync ORM                          djox async (djox.connections)
  ───────────────                          ─────────────────────────────
  thread 1 ─► djox.Connection ─┐           writer queue ─► writer thread ─┐
  thread 2 ─► djox.Connection ─┼─► djox's SQLite ◄─────────────────────────┤
                               │           reader queue ─► reader threads ─┘
                               └─► app.sqlite3 (one SQLite copy, one set of locks)
```

- **One SQLite in the process.** Python's `sqlite3` module has its own copy
  of SQLite. Two copies can't see each other's file locks, so writes from
  both at once can be lost. With djox's `ENGINE`, nothing opens the file
  through Python's copy.
- **Each Django thread keeps its own connection**, used on that thread, as in
  Django. Transactions, savepoints and `TestCase` rollbacks mean exactly what
  they mean in Django. While SQLite works, the GIL is released, so other
  Python threads run.
- **Django's SQL functions.** Django's SQL for date lookups, regular
  expressions and some aggregates calls Python functions that Django
  registers on each connection (`django_date_extract`, `REGEXP`,
  `STDDEV_POP`...). djox calls Django's own `register()` on every connection,
  sync and async; SQLite calls the functions back from Rust, which takes the
  GIL for each call.
- **Sync and async writers take turns** through SQLite's own write lock.
  Transactions start with `BEGIN IMMEDIATE`, so a writer that finds the lock
  taken waits (up to `timeout`) instead of failing.

## The code

| Path                | What it is                                                          |
|---------------------|---------------------------------------------------------------------|
| `crates/djox-core`  | The shared types and the `Database` trait every backend implements  |
| `crates/djox`       | The backends, one folder each: `sqlite/` today, `postgres/` later    |
| `src/*.rs`          | The Python extension `djox._core`: `Database`, `Connection`, `Cursor` |
| `src/djox`          | The Python package, with Django's `ENGINE` in `backends/sqlite3`      |

Each backend owns everything about its database. Postgres will be a new
folder with its own connection pool, behind the same `Database` trait, and
`djox.Database.postgres(...)` next to `djox.Database.sqlite(...)`.

The Rust code is written for Python developers who have never seen Rust:
plain names, straight-line code, and comments that explain each Rust feature
the first time it appears.
