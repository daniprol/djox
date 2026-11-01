# Use djox with Django

Point Django's database at djox's `ENGINE`:

```python
# settings.py
DATABASES = {
    "default": {
        "ENGINE": "djox.backends.sqlite3",  # was "django.db.backends.sqlite3"
        "NAME": BASE_DIR / "db.sqlite3",
    }
}
```

Everything keeps working: the ORM, migrations, the admin, `manage.py test`,
transactions. The difference is underneath: every query now runs on the
SQLite bundled in djox, instead of the one inside Python's `sqlite3` module.
On Postgres, use `"djox.backends.postgresql"` (see [Postgres](#postgres)).

## Why it matters

A process that uses djox has two copies of SQLite: djox's and Python's. Two
copies can't see each other's locks (the operating system drops all of a
process's locks on a file when *any* handle to it closes, and only one copy
of SQLite knows how to work around that). So if Django's sync ORM and djox
write to the same file at once, writes can be lost without an error.

With djox's `ENGINE`, Django's sync connections are djox connections, so
there is one SQLite in the process and every write is kept.

## Async queries: `djox.Manager`

Give a model `djox.Manager()`, and its async QuerySet methods run on djox
(for models you can't edit, see [Existing models](#existing-models-djox_models)):

```python
import djox
from django.db import models


class Book(models.Model):
    title = models.CharField(max_length=200)
    pages = models.IntegerField()

    objects = djox.Manager()


book = await Book.objects.aget(title="Dune")
long_books = [book async for book in Book.objects.filter(pages__gt=400)]
```

Django's own async methods run the sync ORM on a worker thread, and all of
them share *one* thread, so they wait for each other. With `djox.Manager`,
Django still writes the SQL and builds the model instances (so the SQL and
the results are exactly Django's), but the SQL runs on djox's async database
and the event loop never waits for a thread.

| Method | Runs on |
|--------|---------|
| `async for`, `aget()`, `afirst()`, `alast()`, `aearliest()`, `alatest()` | djox |
| `aiterator(chunk_size=2000)` | djox, a chunk of rows at a time (below) |
| `acount()`, `aexists()`, `acontains()`, `aaggregate()` | djox |
| `acreate()` | djox (signals are sent with `asend()`) |
| `aupdate()` | djox |
| `adelete()` | djox when Django can delete with one statement (no `on_delete` work and no delete signal receivers); else a djox thread |
| `aget_or_create()` | djox for the lookup; a djox thread to create |
| `aupdate_or_create()`, `abulk_create()`, `abulk_update()`, `ain_bulk()`, `aexplain()` | a djox thread |
| anything with `prefetch_related()` | a djox thread |
| models with parent models (multi-table inheritance): `acreate()`, `aupdate()` | a djox thread |
| model methods `asave()`, `adelete()`, `arefresh_from_db()` | a djox thread, on a `djox.Model` (below) or a model switched by `DJOX_MODELS`; Django's worker thread otherwise |
| related managers: `author.books.acreate()`, `pizza.toppings.aadd()`, `aremove()`, `aclear()`, `aset()`, `aget_or_create()`, `aupdate_or_create()` | a djox thread (their queries, like `author.books.filter(...)`, run on djox) |

"A djox thread" means Django's own sync method, unchanged, on one of djox's
threads: a small pool, each thread keeping its connection open. Those calls
don't queue behind each other or behind Django's single worker thread.
Writes on one SQLite file take turns (SQLite has one writer, see
[Writes take turns](#writes-take-turns)), so twenty tasks running
`aget_or_create()` for the same row at once create it once. Inside a
transaction (`djox.atomic()`, or Django's `TestCase`) they run on the
transaction's thread, so they join it.

A pooled thread looks after its connections like Django looks after a
request thread's: once per request, before its next call, it runs Django's
`close_old_connections()`. So `CONN_MAX_AGE` and `CONN_HEALTH_CHECKS` work as
usual: with `CONN_MAX_AGE = 0` (Django's default) a pooled thread opens a
fresh connection after each request, and with `CONN_MAX_AGE = None` it keeps
one for good. A program without requests (a script) keeps them open.

On a database whose `ENGINE` isn't djox's, every method is Django's own. A
custom `QuerySet` can subclass `djox.QuerySet` and use
`MyQuerySet.as_manager()`.

### Streaming with `aiterator()`

`async for book in Book.objects.aiterator(chunk_size=2000)` reads the rows
from the database a chunk at a time: the next chunk is read when the loop
has used the previous one, so a query with a million rows never holds them
all in memory. The stream reads on a connection of its own, so other
queries (and writes) never wait for it, even while the loop is halfway
through. It sees the database as it was when it started.

Inside a sync transaction (below), the rows are read all at once on the
transaction's connection, the only one that sees its writes.

### The fast lane

Simple queries skip Django's query building and SQL compiler, and get their
results built in Rust. They are two to four times faster than the same
method with Django writing every query, sync or async
([benchmarks](https://github.com/daniprol/djox/tree/main/benchmarks)).
A query takes the fast lane when it is:

- filtered with `filter(name=value)` on the model's own columns (foreign keys
  included, by instance or id), with `exact`, `gt`, `gte`, `lt`, `lte`, `in`
  or `isnull`, and plain values (numbers, text, booleans, dates, decimals,
  UUIDs);
- ordered by columns (`order_by("-pages", "id")`, or the model's
  `Meta.ordering`), reversed, and sliced (`[10:20]`);
- optionally narrowed to some of the model's own columns with `values(...)`
  or `values_list(...)` (`flat=True` included);
- and then read or written with one of these methods:

| Kind | Async | Sync |
|------|-------|------|
| Rows | `async for`, `aiterator()`, `aget()`, `afirst()`, `alast()` | `for`, `list()`, `iterator()`, `get()`, `first()`, `last()`, `queryset[3]` |
| Answers | `acount()`, `aexists()`, `acontains()` | `count()`, `exists()`, `contains()` |
| Writes | `aupdate(name=value)`, `adelete()` | `update(name=value)`, `delete()` |

`update()` takes plain values for the model's own columns (no `F()`
expressions). `delete()` takes the fast lane when Django would delete with
one statement too: no `on_delete` work and no delete signal receivers.

The SQL is the one Django writes for the same query: djox has Django write it
the first time it meets each *shape* of query (the same filters, ordering,
slice and columns, with any values), and keeps it. Rust then builds the
results: model instances, dicts or tuples, converting dates, times,
decimals and booleans the way Django's converters do (and leaving any value
it doesn't handle to Django's converter). Everything else (`exclude()`,
`Q()`, `F()`, joins like `author__name`, `annotate()`, `select_related()`,
models with parent models) runs as described above, with the same results.
Errors are raised where Django raises them: a wrong field name or value
still raises at `filter()`.

Sync methods on the fast lane run on the thread's Django connection, like
Django's own: they join its transaction, appear in its query log, and refuse
to run in async code (`SynchronousOnlyOperation`).

To turn it off, set `DJOX_FAST_LANE = False` in the settings.

### Model methods: `djox.Model`

A model's own `asave()`, `adelete()` and `arefresh_from_db()` don't go
through its manager. Inherit from `djox.Model` instead of `models.Model` to
run them on djox's threads; it also sets `objects = djox.Manager()`:

```python
class Author(djox.Model):
    name = models.CharField(max_length=100)


author = Author(name="Frank Herbert")
await author.asave()
```

`djox.Model` is abstract (it has no table), so it changes no migration.

### Existing models: `DJOX_MODELS`

Models you can't (or don't want to) edit, like `django.contrib.auth`'s
`User` or another library's models, can be switched to djox by
configuration. Add `"djox"` to `INSTALLED_APPS`:

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "djox",
]
DJOX_MODELS = "__all__"  # the default: every model
# DJOX_MODELS = ["auth", "shop.Order"]  # or some apps and models
# DJOX_MODELS = []  # or none
```

When Django starts, djox switches each chosen model:

- its managers (`objects`, and any custom manager) return `djox.QuerySet`s,
  so their async methods run on djox. A custom `QuerySet` keeps its own
  methods (`Shelf.objects.wide()` still works); djox fills in the async
  methods it didn't write itself;
- its related managers (`user.groups.aadd(...)`) run on djox's threads;
- its `asave()`, `adelete()` and `arefresh_from_db()` become `djox.Model`'s,
  unless the model defines its own.

The model's code, its tables and its migrations don't change: a switched
manager still has its own class name, so `makemigrations` and type checkers
see the model as before. A model that already uses `djox.Manager` or
`djox.Model` is left as it is, and on a database whose `ENGINE` isn't djox's
every method keeps Django's behaviour.

### Inside a sync transaction

Async code that runs inside a sync `atomic()` block (Django's `TestCase`
wraps each test in one, and runs async tests with `async_to_sync()`) must see
the block's writes, and its own writes must belong to the block. So when
`djox.Manager` finds an open sync transaction in the current context, it runs
the SQL on that transaction's connection, on a worker thread. Unrelated async
code, like another request, doesn't see it and keeps using djox's async
database.

## Async transactions: `djox.atomic()`

`djox.atomic()` is Django's `transaction.atomic()` for async code. Everything
inside the block is committed together when it ends, or rolled back if it
raises:

```python
import djox

async with djox.atomic():
    author = await Author.objects.acreate(name="Frank Herbert")
    await Book.objects.acreate(title="Dune", author=author)
```

It takes Django's arguments (`using`, `savepoint`, `durable`) and also works
as a decorator, `@djox.atomic` or `@djox.atomic(using="other")`, on an
`async def`. A nested block is a savepoint: if it raises, only its own work
is undone.

`await djox.on_commit(send_welcome_email)` runs a (sync) function once the
transaction commits, like Django's `transaction.on_commit()`: never if it
rolls back, and right away outside a transaction.

Everything inside the block joins the transaction:

- `djox.Manager` queries run on the transaction's connection;
- Django's own async methods (`aget_or_create()`...) and your own
  `sync_to_async()` functions run on the transaction's thread, so Django's
  sync code there (`transaction.set_rollback(True)`, `Model.save()`...) is in
  the transaction too;
- tasks started inside the block (`asyncio.gather()`) share it. They must
  finish before the block ends.

How it works: the outermost block takes a thread from a small pool (each
keeps its Django connection open) and runs Django's own `atomic()` there.
asgiref's `ThreadSensitiveContext` sends the block's thread-sensitive work to
that thread, so blocks don't queue for Django's single worker thread. Inside
a sync transaction (Django's `TestCase`), the block joins it as a savepoint.

SQLite runs one write transaction at a time, and `BEGIN IMMEDIATE` (djox's
default `transaction_mode`) takes the write lock when the block starts. So
blocks on the same database take turns with each other and with plain writes
(below). Keep blocks short, and don't wait for slow things (an HTTP call)
inside one.

### Writes take turns

SQLite lets one connection write at a time. djox writes through two kinds:

- **plain writes** (`acreate()`, `aupdate()`, a one-statement `adelete()`)
  go to djox's writer thread, which runs them one after the other;
- **transactions** (`djox.atomic()` blocks, and Django's methods on djox's
  threads like `aget_or_create()`) each run on a Django connection of their
  own.

When both write at once, SQLite would make one of them sleep and retry, with
sleeps of up to 100 ms. Instead, djox has them take turns in asyncio, before
they reach SQLite: plain writes share the turn, a transaction holds it alone.
A waiting writer starts the moment the turn is free, and gives up with
"database is locked" after the `timeout` option, like SQLite would. Plain
writes that keep coming can't hold a transaction back: after a couple of
milliseconds, new ones wait behind it.

This covers the writes of one event loop. Writes from sync Django code (other
threads) or other processes still meet SQLite's own waiting.

A block costs two thread round trips (start and end), plus one per query
inside. For a short transaction, wrapping a sync function in `sync_to_async()`
is still faster ([benchmarks](https://github.com/daniprol/djox/tree/main/benchmarks)); `djox.atomic()` lets the code
inside stay async, and keeps Django's worker thread free for everything else.

### Errors and the query log

Errors are Django's (`django.db.IntegrityError`...), and queries appear in
`connection.queries` with `DEBUG = True`, like with Django's cursors. Inside a
sync transaction, they are logged on that transaction's connection, so
`assertNumQueries()` around `async_to_sync(...)` counts them.

## Hooks

Hooks are your functions, called around every SQL statement that runs on
djox's `ENGINE`: Django's sync ORM, every `djox.QuerySet` method (the fast
lane included), from sync or async code. A hook can watch the statements
(time them, log them, count them per request) or change them (add a comment
for tracing, block writes to a read-only replica):

```python
import logging

import djox

logger = logging.getLogger(__name__)


def tag_with_service(query: djox.Query) -> djox.Query:
    return query.with_sql(f"/* service: shop */ {query.sql}")


def warn_if_slow(query: djox.Query, finished: djox.QueryFinished) -> None:
    if finished.seconds > 0.1:
        logger.warning("slow query (%.3fs): %s", finished.seconds, query.sql)


djox.add_hooks(djox.Hooks(before_query=tag_with_service, after_query=warn_if_slow))
```

| Hook | When | Returns |
|------|------|---------|
| `before_query(query)` | before each statement | `None` to change nothing, or a new `djox.Query` to run instead |
| `after_query(query, finished)` | after each statement, also when it failed (`finished.error`) | `None` |

A `djox.Query` has the `alias`, the `sql` (with `%s` placeholders), the
`parameters`, and `many` (for `executemany()`); `query.with_sql(...)` and
`query.with_parameters(...)` make a changed copy. Hooks run in the order
they were added, each `before_query` seeing what the previous one returned.
Raising in `before_query` stops the statement, and the caller gets the
error. `djox.add_hooks()` takes `djox.Hooks(...)` or any object with
`before_query` and/or `after_query` methods; `djox.remove_hooks()` takes it
away again.

Hooks are plain functions, not `async def`: they run inside every query,
also in sync code, where nothing can be awaited. A streamed `aiterator()` is
one statement: the hooks see it once, when its first chunk arrives.
Django's signals (`pre_save`, `post_delete`...) keep working as always, and
`djox.connections[alias]` (the layer below the ORM) has no hooks.

## The async database of an alias

`djox.connections["default"]` gives the async [`djox.Database`](index.md) for
the same settings: one writer and a few readers, shared by the whole process.

```python
import djox

database = djox.connections["default"]
rows = await database.fetch_all("SELECT title FROM library_book WHERE pages > %s", [300])
```

Its connections understand the same SQL as Django's: Django's SQL functions
(`django_date_extract`, `REGEXP`, `STDDEV_POP`...) are registered on every
one of them, so SQL from Django's compiler runs unchanged.

## Options

`OPTIONS` takes Django's SQLite options, plus one for the async database:

| Option             | Default       | What it does                                                    |
|--------------------|---------------|-----------------------------------------------------------------|
| `timeout`          | `5`           | Seconds to wait for a lock held by another connection           |
| `transaction_mode` | `"IMMEDIATE"` | How `atomic()` starts a transaction: `BEGIN <mode>`. `None` for a plain `BEGIN` |
| `init_command`     | `""`          | SQL run on every new connection, `;`-separated                  |
| `readers`          | 1 per core, at most 4 | Reader connections of `djox.connections[alias]`          |

An unknown option (like `detect_types`, an option of Python's `sqlite3`)
raises `ImproperlyConfigured`.

## What is different from Django's SQLite backend

- **WAL mode.** The database file is switched to WAL mode (write-ahead log),
  so reads and writes don't wait for each other. WAL keeps two files next to
  the database while it is open: `db.sqlite3-wal` and `db.sqlite3-shm`.
  `synchronous` is `NORMAL`: a power cut can lose the last commits, never
  corrupt the file.
- **`transaction_mode` is `IMMEDIATE` by default.** A transaction takes the
  write lock at `BEGIN`. With SQLite's default (`DEFERRED`), a transaction
  that reads and then writes can fail at once with "database is locked" when
  another connection wrote meanwhile; with djox there are always several
  connections.
- **Test databases are files.** Django tests SQLite on an in-memory database,
  which only one connection can see. djox's test database is
  `test_db.sqlite3` next to `NAME` (or `djox_test_<alias>.sqlite3` in the temp
  folder when `NAME` is `":memory:"`). `TEST["NAME"]` still wins. With WAL
  mode and `synchronous = NORMAL`, a commit doesn't wait for the disk, so a
  file costs little: djox's own test suite runs about 10% faster with its
  test database in memory (a RAM disk, `TEST = {"NAME":
  "/dev/shm/test_db.sqlite3"}` on Linux) than on disk.
- **Raw cursors return SQLite's own types.** `cursor.execute("SELECT
  published FROM ...")` gives the stored text, not a `datetime` (Django's
  backend asks Python's `sqlite3` to parse some column types). The ORM is not
  affected: its converters parse the text, as they always did.
- **Math functions run in SQLite.** `Power`, `Sqrt`, `ACos`... are built into
  djox's SQLite, instead of being Python functions.

## Postgres

djox has a Postgres `ENGINE` too:

```python
# settings.py
DATABASES = {
    "default": {
        "ENGINE": "djox.backends.postgresql",  # was "django.db.backends.postgresql"
        "NAME": "app",
        "USER": "django",
        "PASSWORD": "...",
        "HOST": "localhost",
        "PORT": 5432,
        "OPTIONS": {"async_pool_size": 10},
    }
}
```

It is Django's own Postgres backend for everything sync (migrations, the
admin, the sync ORM, `manage.py test`), still on psycopg, which you install
as usual (`uv add "psycopg[binary]"`). Async code (`djox.Manager`,
`djox.atomic()`, `djox.connections[alias]`) runs on a pool of connections
that Rust owns, so `await Book.objects.aget(...)` never takes a thread. The
two drivers are simply two clients of the same server, so, unlike with
SQLite, they can't lose each other's writes.

Everything above works the same: the fast lane, streaming `aiterator()`,
hooks, `djox.atomic()` and queries inside a sync transaction. Writers don't
take turns: Postgres locks rows, so it lets many transactions write at once.

`OPTIONS` are psycopg's, as with Django's backend, plus one of djox's:

| Option            | Default | What it does                                                  |
|-------------------|---------|---------------------------------------------------------------|
| `async_pool_size` | `10`    | The most connections `djox.connections[alias]` opens at once  |

The async pool connects with the same `NAME`, `USER`, `PASSWORD`, `HOST`,
`PORT` and libpq settings (`connect_timeout`, `application_name`,
`options`, `target_session_attrs`...), and uses the same time zone as
Django's connections. Values come back as the same Python objects psycopg
gives Django (`Decimal`, aware `datetime`, `UUID`, JSON as text...).

Not supported yet, so these raise `ImproperlyConfigured` instead of being
quietly ignored:

- **TLS.** `sslmode` must be `disable`, `allow` or `prefer` (the async pool
  doesn't encrypt). Use it on a private network or through a local proxy
  for now.
- **`service`** (`pg_service.conf`) and **`assume_role`**.

Reading array, range and `hstore` columns from async code isn't supported
yet either: the query raises `ProgrammingError` ("djox can't read the
Postgres type ... yet"). Sync queries are unaffected.

## Good to know

- Django's sync ORM still blocks the thread it runs on, as always. While
  SQLite works, other Python threads keep running.
- Models with Django's default manager keep Django's async methods (a worker
  thread), unless `"djox"` is in `INSTALLED_APPS` (see `DJOX_MODELS`) or the
  model uses `djox.Manager()`.
- `djox.connections[alias]` used directly doesn't see an open transaction of
  Django's sync connection (like the one Django's `TestCase` wraps each test
  in); `djox.Manager` does. For raw SQL in such tests, use
  `TransactionTestCase` (or `@pytest.mark.django_db(transaction=True)`).
- Inside a sync `atomic()` block run by the event loop's own thread (an
  `asyncio.run()` inside `atomic()`, or a pytest-asyncio test without
  `transaction=True`), nothing else can use the transaction's connection:
  `djox.atomic()` raises `TransactionManagementError`. Django's `TestCase`
  works, because it runs async tests with `async_to_sync()`.
