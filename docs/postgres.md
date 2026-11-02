# Postgres

```bash
uv add djox "psycopg[binary]"
```

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

- **Sync code is Django's own Postgres backend**, on psycopg: migrations, the
  admin, the sync ORM, `manage.py test`.
- **Async code runs on a pool of connections that Rust owns**: djox's
  QuerySets, `djox.atomic()`, `djox.connections[alias]`. So
  `await Book.objects.aget(...)` never takes a thread.
- **The two are just two clients of the server**, so, unlike with SQLite, they
  can't lose each other's writes.
- **Everything else works the same**: the fast lane, streaming, hooks,
  `djox.atomic()`, queries inside `TestCase`. Writers don't take turns:
  Postgres locks rows, so many transactions write at once.

## Options

`OPTIONS` are psycopg's, as with Django's backend, plus one of djox's:

| Option            | Default | What it does                                                  |
|-------------------|---------|---------------------------------------------------------------|
| `async_pool_size` | `10`    | The most connections `djox.connections[alias]` opens at once  |

The async pool:

- connects with the same `NAME`, `USER`, `PASSWORD`, `HOST`, `PORT` and libpq
  settings (`connect_timeout`, `application_name`, `options`,
  `target_session_attrs`...);
- uses the same time zone as Django's connections;
- returns the same Python objects psycopg gives Django (`Decimal`, aware
  `datetime`, `UUID`, JSON as text...).

## Not supported yet

These raise `ImproperlyConfigured` instead of being quietly ignored:

- **TLS.** `sslmode` must be `disable`, `allow` or `prefer` (the async pool
  doesn't encrypt yet). Use a private network or a local proxy for now.
- **`service`** (`pg_service.conf`) and **`assume_role`**.

And from async code:

- **Array, range and `hstore` columns** raise `ProgrammingError` ("djox can't
  read the Postgres type ... yet"). Sync queries are unaffected.
