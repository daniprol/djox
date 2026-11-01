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
- Django's `aget()`, `acount()`... keep running Django's sync ORM on a worker
  thread for now. Running them truly async on djox is the next step of the
  [roadmap](roadmap.md).
- `djox.connections[alias]` doesn't see an open transaction of Django's sync
  connection (like the one Django's `TestCase` wraps each test in). Use
  `TransactionTestCase` (or `@pytest.mark.django_db(transaction=True)`) for
  tests that mix the two.
