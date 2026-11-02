# Postgres

```bash
uv add "djox[postgres]"  # or: pip install "djox[postgres]"
```

The `postgres` extra installs psycopg 3 for Django's sync code. psycopg2 isn't
supported: djox raises `ImproperlyConfigured` without psycopg 3.

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

## TLS

Set libpq's TLS options; Django's connections and djox's use the same ones:

```python
"OPTIONS": {
    "sslmode": "verify-full",
    "sslrootcert": BASE_DIR / "rds-ca.pem",  # or "system", the default
},
```

| `sslmode`     | Encrypted?                 | Certificate checked?                              |
|---------------|----------------------------|---------------------------------------------------|
| `disable`     | no                         |                                                   |
| `allow`       | if the server offers it    | no (djox tries TLS first, libpq last)             |
| `prefer`      | if the server offers it    | no (the default)                                  |
| `require`     | yes                        | no, unless `sslrootcert` is given: then like `verify-ca` |
| `verify-ca`   | yes                        | signed by a trusted authority                     |
| `verify-full` | yes                        | signed by a trusted authority, for this host name |

- **Use `verify-full` across networks you don't control.** Without a check,
  someone on the network could pretend to be the server.
- **`sslrootcert`** is a PEM file of the authorities to trust, like your
  provider's bundle, or `"system"` for the ones your operating system trusts.
  djox uses the system's when it isn't given (libpq looks for
  `~/.postgresql/root.crt` instead, so give it explicitly to be sure both
  agree).
- **`sslcert` and `sslkey`** are a client certificate and its key (PEM
  files), for servers that ask for one.
- The encryption is [rustls](https://github.com/rustls/rustls), in Rust:
  nothing to install.

## Not supported yet

These raise `ImproperlyConfigured` instead of being quietly ignored:

- **`service`** (`pg_service.conf`) and **`assume_role`**.
- **`sslpassword`** (an encrypted key), **`sslcrl`** and **`sslcrldir`**.

djox reads `DATABASES`, not libpq's environment variables (`PGSSLMODE`,
`PGHOST`...): put the settings in `DATABASES`.

And from async code:

- **Array, range and `hstore` columns** raise `ProgrammingError` ("djox can't
  read the Postgres type ... yet"). Sync queries are unaffected.
