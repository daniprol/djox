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

## Django's async queries

Django's own async methods (`aget()`, `acount()`...) run the whole sync
method on a worker thread, and every one of them shares the same thread.
`djox.Manager` hands out `djox.QuerySet`s, whose async methods split the work:

```text
  event loop thread                                   djox's threads
  ─────────────────                                   ──────────────
  Book.objects.filter(pages__gt=400)    build the query (Django)
  compiler.as_sql()                     write the SQL (Django)
  await ──────────────────────────────────────────────► reader runs the SELECT
       ◄────────────────────────────────────────────── rows
  ModelIterable(rows)                   build the Book objects (Django)
```

The SQL and the objects are exactly Django's: djox only takes over the moment
the SQL runs. How it gets that moment out of Django's code depends on the
method:

- **Reading rows** (`async for`, `aget()`, `afirst()`...): djox compiles the
  `SELECT` itself, awaits the rows, then hands Django's iterable class
  (`ModelIterable`, `ValuesIterable`...) the compiler with the rows already
  in it. Nothing is compiled twice.
- **First statement** (`acount()`, `aexists()`, `aupdate()`, fast
  `adelete()`): djox runs Django's sync method. When it reaches its
  statement, djox's compiler stops it (it raises `StatementNeeded`) and djox
  awaits the statement. What is left of the method is a line or two (`count()`
  returns the first column), which djox does itself.
- **Replay** (`aaggregate()`): the same, then Django's method runs a second
  time and gets the answer handed to it, so Django shapes the result. It only
  builds a query before running it, so running it twice is safe.
- **`acreate()`**: `save()` sends signals and fills defaults (`auto_now`
  dates), which must happen once. djox sends the signals with `asend()`, runs
  Django's `_save_table()` once, takes the `INSERT` from it, and awaits it.

Methods that need a transaction or several statements (`aget_or_create()`,
`abulk_create()`, deletes that cascade, `prefetch_related()`, `asave()` on a
`djox.Model`) run Django's sync method unchanged, on a small pool of djox's
own threads (`djox/_threads.py`), instead of Django's single worker thread.
Each pooled thread keeps its Django connection open. Writers on one SQLite
file take turns in asyncio, because SQLite has one writer, and its own way of
waiting (sleeping and retrying) is slow.

When async code runs inside a sync `atomic()` block (Django's `TestCase`),
its SQL runs on that block's connection instead, so it sees the block's
writes. djox's `DatabaseWrapper` notes each open transaction in a context
variable, which `async_to_sync()` copies to the async code it starts.

## Async transactions

A transaction belongs to one connection, and Django keeps one connection per
thread. So `djox.atomic()` gives the block a thread:

```text
  event loop thread                          the block's thread (pooled)
  ─────────────────                          ───────────────────────────
  async with djox.atomic():  ───────────────► Django's atomic().__enter__ (BEGIN)
      await Author.objects.acreate(...)  ───► INSERT on that thread's connection
      await Author.objects.aget_or_create() ► Django's get_or_create(), same thread
                             ───────────────► Django's atomic().__exit__ (COMMIT)
```

Django's own `atomic()` does the transaction work (savepoints, rollback on
error, `on_commit`), so it behaves exactly like in sync code. asgiref's
`ThreadSensitiveContext` routes every thread-sensitive call of the block
(`sync_to_async()`, Django's async methods) to the block's thread. The block's
thread comes from the same pool, and blocks take their turn like other
writers.

## The code

| Path                | What it is                                                          |
|---------------------|---------------------------------------------------------------------|
| `crates/djox-core`  | The shared types and the `Database` trait every backend implements  |
| `crates/djox`       | The backends, one folder each: `sqlite/` today, `postgres/` later    |
| `src/*.rs`          | The Python extension `djox._core`: `Database`, `Connection`, `Cursor` |
| `src/djox`          | The Python package: Django's `ENGINE` in `backends/sqlite3`, `djox.Manager` and `djox.QuerySet` |

Each backend owns everything about its database. Postgres will be a new
folder with its own connection pool, behind the same `Database` trait, and
`djox.Database.postgres(...)` next to `djox.Database.sqlite(...)`.

The Rust code is written for Python developers who have never seen Rust:
plain names, straight-line code, and comments that explain each Rust feature
the first time it appears.
