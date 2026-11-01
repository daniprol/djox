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

## Why it matters

A process that uses djox has two copies of SQLite: djox's and Python's. Two
copies can't see each other's locks (the operating system drops all of a
process's locks on a file when *any* handle to it closes, and only one copy
of SQLite knows how to work around that). So if Django's sync ORM and djox
write to the same file at once, writes can be lost without an error.

With djox's `ENGINE`, Django's sync connections are djox connections, so
there is one SQLite in the process and every write is kept.

## Async queries: `djox.Manager`

Give a model `djox.Manager()`, and its async QuerySet methods run on djox:

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
| `async for`, `aiterator()`, `aget()`, `afirst()`, `alast()`, `aearliest()`, `alatest()` | djox |
| `acount()`, `aexists()`, `acontains()`, `aaggregate()` | djox |
| `acreate()` | djox (signals are sent with `asend()`) |
| `aupdate()` | djox |
| `adelete()` | djox when Django can delete with one statement (no `on_delete` work and no delete signal receivers); else Django |
| `aget_or_create()`, `aupdate_or_create()`, `abulk_create()`, `abulk_update()`, `ain_bulk()`, `aexplain()` | Django's worker thread, for now |
| anything with `prefetch_related()` | Django's worker thread, for now |
| models with parent models (multi-table inheritance): `acreate()`, `aupdate()` | Django's worker thread, for now |
| model instance methods: `asave()`, `adelete()`, `arefresh_from_db()` | Django's (they don't go through the manager) |

"Django's worker thread" means the method behaves exactly as without djox:
nothing breaks, it is only not faster yet. The same happens on a database
whose `ENGINE` isn't djox's. A custom `QuerySet` can subclass `djox.QuerySet`
and use `MyQuerySet.as_manager()`.

### Inside a sync transaction

Async code that runs inside a sync `atomic()` block (Django's `TestCase`
wraps each test in one, and runs async tests with `async_to_sync()`) must see
the block's writes, and its own writes must belong to the block. So when
`djox.Manager` finds an open sync transaction in the current context, it runs
the SQL on that transaction's connection, on a worker thread. Unrelated async
code, like another request, doesn't see it and keeps using djox's async
database.

### Errors and the query log

Errors are Django's (`django.db.IntegrityError`...), and queries appear in
`connection.queries` with `DEBUG = True`, like with Django's cursors. Inside a
sync transaction, they are logged on that transaction's connection, so
`assertNumQueries()` around `async_to_sync(...)` counts them.

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
  folder when `NAME` is `":memory:"`). `TEST["NAME"]` still wins.
- **Raw cursors return SQLite's own types.** `cursor.execute("SELECT
  published FROM ...")` gives the stored text, not a `datetime` (Django's
  backend asks Python's `sqlite3` to parse some column types). The ORM is not
  affected: its converters parse the text, as they always did.
- **Math functions run in SQLite.** `Power`, `Sqrt`, `ACos`... are built into
  djox's SQLite, instead of being Python functions.

## Good to know

- Django's sync ORM still blocks the thread it runs on, as always. While
  SQLite works, other Python threads keep running.
- Models with Django's default manager keep Django's async methods (a worker
  thread). Use `djox.Manager()` to run them on djox.
- `djox.connections[alias]` used directly doesn't see an open transaction of
  Django's sync connection (like the one Django's `TestCase` wraps each test
  in); `djox.Manager` does. For raw SQL in such tests, use
  `TransactionTestCase` (or `@pytest.mark.django_db(transaction=True)`).
- Async transactions (`async with atomic()`) are the next step of the
  [roadmap](roadmap.md).
