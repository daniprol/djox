# Raw SQL

`djox.Database` runs SQL from async code, with or without Django.

## Without Django

```python
--8<-- "docs/examples/quickstart.py"
```

- **Open it**: `djox.Database.sqlite(path, readers=4)` (one writer, a few
  readers) or `djox.Database.postgres(url, pool_size=10)` (a pool). Use it in a
  `with` block, or call `close()`.
- **TLS on Postgres**: libpq's settings, as arguments:
  `djox.Database.postgres(url, sslmode="verify-full", sslrootcert="ca.pem")`
  (see [Postgres](postgres.md#tls)).
- **Methods**: `execute()`, `execute_many()`, `fetch_all()`, `fetch_one()`.
- **Placeholders are Django's**: `%s` (`%%` for a literal `%`).
- **Futures**: each method sends the statement right away and returns an
  `asyncio.Future`. Await it, or pass several to `asyncio.gather()`.
- **Rows are tuples** of `None`, `int`, `float`, `str` and `bytes` on SQLite,
  like Python's `sqlite3`. Parameters can also be `bool`, `datetime`, `date`,
  `time`, `Decimal` and `UUID`, stored the way Django stores them.

## Errors

They follow PEP 249, like Python's `sqlite3`:

| Error | When |
|---|---|
| `djox.IntegrityError` | a constraint failed |
| `djox.OperationalError` | the database couldn't run the statement |
| `djox.ProgrammingError` | a wrong number of parameters... |
| `djox.DataError` | a value doesn't fit its column |
| `djox.DatabaseClosedError` | after `close()` |

The database's errors are subclasses of `djox.DatabaseError`; all of them,
of `djox.Error`.

## Next to Django

`djox.connections[alias]` is the async `Database` for the same settings as a
Django alias, shared by the whole process:

```python
import djox

database = djox.connections["default"]
rows = await database.fetch_all("SELECT title FROM shop_book WHERE pages > %s", [300])
```

- **Same SQL as Django**: Django's SQL functions (`django_date_extract`,
  `REGEXP`, `STDDEV_POP`...) are registered on every connection, so SQL from
  Django's compiler runs unchanged.
- **No hooks, no transaction**: it is the layer below the ORM. It doesn't
  see an open sync transaction (like `TestCase`'s), and hooks don't run.

!!! danger "SQLite: use djox's `ENGINE` next to Django"
    A `djox.Database` and Python's `sqlite3` must never open the same file
    ([why](sqlite.md#why-the-engine-matters)). With
    `"ENGINE": "djox.backends.sqlite3"`, Django uses djox's SQLite too.
