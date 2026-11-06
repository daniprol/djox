# SQLite

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
transactions. Underneath, every query runs on the SQLite bundled in djox.

- **One writer, a few readers**: djox's async connections are one writer
  thread and a few reader threads, in WAL mode, so reads never wait for
  writes.
- **No "database is locked" under load**: djox's writers
  [take turns](transactions.md#writes-take-turns) instead of sleeping and
  retrying.
- **Quick reads**: a small async read (one row by its id) runs right on the
  calling thread, which is ten times faster than the trip to a reader thread.
  SQLite stops it after `quick_read_limit` (1 ms), and it then runs on a
  reader, so the event loop is never held longer.
- **Quick writes**: a small async write (`aupdate()` of one row, `acreate()`)
  runs right on the calling thread too, on the writer connection itself,
  when the writer is free. It never waits for a lock, and SQLite stops it
  after `quick_write_limit` (1 ms); it then goes to the writer thread.
  `djox.atomic()` blocks run their statements the same way
  ([transactions](transactions.md)).

## Why the `ENGINE` matters

!!! danger "Never open djox's database with Python's `sqlite3`"
    djox bundles its own copy of SQLite, and Python's `sqlite3` module has
    another. Two copies in one process can't see each other's locks, so if
    both write to the same file, writes can be lost without an error.

    With djox's `ENGINE`, Django's sync connections are djox connections too:
    one SQLite in the process, and every write is kept.

??? info "Why two copies don't see each other's locks"
    The operating system drops all of a process's locks on a file when *any*
    handle to it closes. SQLite works around that, but only for the handles
    of its own copy.

## Options

`OPTIONS` takes Django's SQLite options, plus two for the async connections:

| Option             | Default       | What it does                                                    |
|--------------------|---------------|-----------------------------------------------------------------|
| `timeout`          | `5`           | Seconds to wait for a lock held by another connection           |
| `transaction_mode` | `"IMMEDIATE"` | How `atomic()` starts a transaction: `BEGIN <mode>`. `None` for a plain `BEGIN` |
| `init_command`     | `""`          | SQL run on every new connection, `;`-separated                  |
| `readers`          | 1 per core, at most 4 | Reader connections of `djox.connections[alias]`          |
| `quick_read_limit` | `0.001`       | Seconds a small async read may run on the calling thread; `0`: never |
| `quick_write_limit` | `0.001`      | The same for a small async write, and the statements of a `djox.atomic()` block |

An unknown option (like `detect_types`, an option of Python's `sqlite3`)
raises `ImproperlyConfigured`.

## What's different from Django's SQLite backend

- **WAL mode.** Reads and writes don't wait for each other. While the database
  is open, two files sit next to it: `db.sqlite3-wal` and `db.sqlite3-shm`.
- **`synchronous = NORMAL`.** A power cut can lose the last commits, never
  corrupt the file.
- **`transaction_mode` is `IMMEDIATE`.** A transaction takes the write lock at
  `BEGIN`. With SQLite's default (`DEFERRED`), a transaction that reads and
  then writes can fail at once with "database is locked" when another
  connection wrote meanwhile, and djox always has several connections.
- **Test databases are files**, not in memory ([Testing](testing.md)).
- **Raw cursors return SQLite's own types.** `cursor.execute("SELECT published
  FROM ...")` gives the stored text, not a `datetime`. The ORM is not affected:
  its converters parse the text, as they always did.
- **Math functions run in SQLite.** `Power`, `Sqrt`, `ACos`... are built into
  djox's SQLite, instead of being Python functions.
