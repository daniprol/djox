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
  A new query wakes the reader that went to sleep last, so queries that come
  one at a time keep landing on the same warm thread, while a burst of
  queries spreads over all of them.
- **Threads are started once.** The writer and the readers start when the
  database opens and live until it closes: no thread is started per query.
  Each one registers with Python the first time it hands back an answer and
  stays registered, so taking the GIL to finish a Future is cheap.
- **WAL mode**: readers never wait for the writer, and the writer never waits
  for readers.
- **`synchronous = NORMAL`**: safe with WAL and much faster than Django's
  default; a power cut can lose the last commits, never corrupt the file.
- **Quick reads.** The trip to a reader and back to the event loop costs 60
  to 80 µs; SQLite answers a small read in about 5. So `fetch_all()` first
  tries the read right on the calling thread, on one more reader connection
  kept for that (`quick_reads.rs`). SQLite stops it after `quick_read_limit`
  (1 ms, with a progress handler), and it runs on a reader instead: nothing
  is lost but that moment. A read that ran out of time skips this for its
  next 100 runs, then gets another chance (twice as many skipped each time
  it runs out again). If another thread is using that connection, the read
  doesn't wait: it goes to a reader. The same connection tells which
  statements write, so an `INSERT ... RETURNING` goes straight to the writer.
- **Streams get a connection of their own.** `stream_rows()` (behind
  `aiterator()`) opens a reader connection on a thread of its own, which
  reads the next chunk each time Python asks for it. A slow loop holds only
  that connection, never one of the shared readers, and the thread ends with
  the stream.

## The Postgres backend

Postgres is a server: a connection is a socket, and many connections can
read and write at once. So there is no single writer: `Database.postgres()`
keeps a pool of connections (`deadpool-postgres`, 10 by default) on a small
runtime of async Rust (`tokio`, two threads) that the database owns.
Python sends a statement, a task borrows a free connection, runs it, and
completes Python's Future; the event loop never waits, and no Python
thread is used.

```text
  Python (asyncio)          Rust (tokio, 2 threads)
  ─────────────────         ──────────────────────────────────────────
  await fetch_all(...) ─►  task: borrow a connection ─► run ─┐
  await execute(...)   ─►  task: borrow a connection ─► run ─┼─► Postgres server
  await execute(...)   ─►  task: borrow a connection ─► run ─┘
```

- **Prepared statements.** Each connection prepares a statement the first
  time it sees its SQL and keeps it, so the same query costs one round trip
  afterwards. Django writes the same SQL for the same query shape, so a few
  hundred statements cover an app.
- **`%s` becomes `$1`.** Django's placeholders are turned into Postgres's
  numbered ones. Numbers go to the server in binary; everything else goes
  as text and the server reads it as the column's type, as psycopg does.
- **Errors are sorted by SQLSTATE class**, like psycopg sorts them: `23...`
  is an `IntegrityError`, `22...` a `DataError`, `42...` a
  `ProgrammingError`.
- **Streams** open a connection of their own, in a read-only transaction,
  and read the query through a portal (a server-side cursor) a chunk at a
  time.

Django's sync code stays on Django's own backend and psycopg: two drivers
on two sockets are two clients of the server, so nothing has to be shared.
Async queries inside a sync transaction run on that transaction's psycopg
connection, as on SQLite.

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
Each pooled thread keeps its Django connection open, and runs Django's
`close_old_connections()` once per request, like a request thread
(`CONN_MAX_AGE`). Related managers' async methods (`author.books.aadd()`) run
there too (`djox/related.py`).

Writers on one SQLite file take turns in asyncio (`djox/_write_turns.py`),
because SQLite has one writer, and its own way of waiting (sleeping and
retrying) is slow. Plain writes, sent to djox's writer thread, share the
turn; a transaction on a Django connection holds it alone.

With `"djox"` in `INSTALLED_APPS`, `djox/install.py` switches the models
`DJOX_MODELS` chooses when Django is ready: each manager's class gets a
djox subclass (same name, so migrations see no change) whose `QuerySet` is
the model's own plus `djox.QuerySet`.

When async code runs inside a sync `atomic()` block (Django's `TestCase`),
its SQL runs on that block's connection instead, so it sees the block's
writes. djox's `DatabaseWrapper` notes each open transaction in a context
variable, which `async_to_sync()` copies to the async code it starts.

## The fast lane

Most of the time of a `djox.Manager` query is Django's Python: building the
`Query` (`filter()`), writing the SQL (the compiler) and building the
instances. SQLite itself takes a few microseconds. For most queries, djox
skips most of it:

```text
  Book.objects.filter(pages__gt=500)[:20]     a recipe: filter(pages__gt=?) [500], slice [0:20]
  async for book in ...:
      shape seen before?  ── no ──►  Django builds the query and writes the SQL, once
              │ yes
      parameters  [500]                the field's own get_prep_value(), like Django's lookup
      await ──────────────────────────────────────► reader runs the SELECT
           ◄────────────────────────────────────── rows
      ResultBuilder (Rust)            one instance (or dict, or tuple) per row
```

- **A recipe instead of a `Query`.** A `djox.QuerySet` writes down each call
  (`filter()`, `annotate()`, `order_by()`, `values()`, a slice...) as a step
  of a recipe (`_fast_lane/recipe.py`), with its values taken out: the
  `500` of `pages__gt=500`, the `1` of `F("pages") + 1`. What is left is the
  call's *shape*. Names, Django's expression classes and their structure
  (`AND`, `OR`, `NOT`) are shape, since they decide the SQL. If anyone asks
  for `queryset.query`, Django's own methods replay the recipe and build it.
- **Errors come at the same moment.** Filter values are prepared by their
  field right away, so a wrong value raises at `filter()`. Other calls are
  checked by Django once per kind of step (`_fast_lane/checks.py`): the first
  `annotate(n=Count("chaptrs"))` raises Django's `FieldError` at the call,
  and a later call of that shape is known to be fine. The check also says
  each annotation's output field (to prepare `n__gt=2`) and whether the query
  groups its rows.
- **The SQL of each shape is Django's.** Two queries that differ only in their
  values have the same shape, and the same SQL. The first time djox meets a
  shape, Django builds the query from the recipe and writes the SQL; djox
  keeps it, with where each parameter comes from (`_fast_lane/parameters.py`):
  each of Django's parameters is matched, by value and type, to one of the
  recipe's values, or is fixed by the shape (a constant Django adds,
  like the `1` that `exists()` selects). If a parameter could come from two values (`pages=412` and
  `F("pages") + 412`), djox waits for a query whose values differ; if a value
  is missing from the SQL (an `alias()` nothing uses), Django keeps the
  shape. Writing the SQL in Rust would buy nothing, since it is written once
  per shape, and this way the SQL is exactly Django's, for every Django
  version and every database.
- **Rust builds the results** (`ResultBuilder`, `src/results.rs`): for a
  model, an empty instance, its `_state`, and one attribute per column. It is
  what `Model.from_db()` makes, without running `__init__` for every row.
  Models whose `__init__` or `from_db()` is their own, or with
  `pre_init`/`post_init` receivers, are built by Django. For `values()` and
  `values_list()`, a dict or a tuple per row.
- **`select_related()` too.** Its relations are part of the shape, so Django
  writes the joins once. Its compiler says which columns belong to which
  related model (`klass_info`, what Django's `RelatedPopulator` reads), and
  Rust builds those objects the same way, from their own columns, then calls
  Django's own setters to cache each one on its parent (`book.author`), and
  the parent on it for a one-to-one. A `NULL` primary key means no related
  object (an empty nullable foreign key).
- **`prefetch_related()` keeps the note.** Its lookups live on the
  `QuerySet`, not in Django's `Query`, so the main query stays on the fast
  lane, on the djox thread that then runs Django's prefetching (`djox/prefetch.py`).
  The prefetching query of a reverse foreign key is on the fast lane too: Django adds its
  `author_id IN (...)` with `query.add_q()`, which would build Django's `Query`, so
  `djox/_prefetch_filter.py` swaps that one Django function for a `filter()` in the recipe.
- **Rust converts the values** (`src/conversions.rs`): text to `datetime`,
  `date` and `time`, a stored number to a `Decimal` rounded like Django
  rounds it, `0`/`1` to `bool`. Only when the column's converter is exactly
  Django's own (`_fast_lane/conversions.py` checks), and any value Rust
  doesn't handle goes to Django's converter. A field's own `from_db_value()`
  always runs.
- **Aggregates and writes too.** `aggregate()`, `update()` (with values or
  expressions) and a `delete()` Django would do in one statement keep a
  statement per shape too, captured from Django's own `aggregate()`,
  `update()` and `_raw_delete()`.
- **New objects too.** `create()`, `acreate()` and `asave()` keep the
  `INSERT` Django wrote for a model's first new object, and compute the next
  ones' values like Django's compiler (`pre_save()`, then
  `get_db_prep_save()`). The rest of `save()` (the related objects' check,
  the signals) is Django's methods, called in Django's order
  (`djox/_saving.py`).
- **Sync methods too.** `for`, `get()`, `count()`, `update()`... use the same
  statements, run through a Django cursor on the thread's connection, so
  they behave like Django's in every way but speed.
- **Anything else is Django's.** Asking for `queryset.query` builds Django's
  `Query` from the note (the same `Q` objects, through Django's own
  `filter()` code), so `F()`, relations to many rows, `annotate()` and
  `only()` work exactly as in Django.

## Hooks

Every statement passes through one of two doors, and both run the hooks
(`djox/hooks.py`):

- **Sync code** goes through Django's cursors, which call every function in
  the connection's `execute_wrappers` around each statement (Django's own
  `connection.execute_wrapper()`). djox's `DatabaseWrapper` adds one that
  runs the hooks.
- **Async code** (`djox.QuerySet`'s methods, the fast lane) goes through
  `djox/_running.py`, which runs them around each `await`.

The list of hooks is a tuple, replaced (never changed) when hooks are added
or removed, so a query running on another thread keeps the list it started
with, without a lock.

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
| `crates/djox`       | The backends, one folder each: `sqlite/` and `postgres/`            |
| `src/*.rs`          | The Python extension `djox._core`: `Database`, `RowStream`, `Connection`, `Cursor`, `ResultBuilder` and its conversions |
| `src/djox`          | The Python package: Django's `ENGINE`s in `backends/sqlite3` and `backends/postgresql`, `djox.Manager` and `djox.QuerySet`, the fast lane in `_fast_lane` |

Each backend owns everything about its database (connections, threads or
pool, value conversions, placeholders, errors) in its own folder, behind the
same `Database` trait: `djox.Database.sqlite(...)` and
`djox.Database.postgres(...)` give the same Python object.

The Rust code is written for Python developers who have never seen Rust:
plain names, straight-line code, and comments that explain each Rust feature
the first time it appears.
