"""Django's Postgres backend, with djox running its async queries.

```python
DATABASES = {
    "default": {
        "ENGINE": "djox.backends.postgresql",
        "NAME": "app",
        "USER": "django",
        "PASSWORD": "...",
        "HOST": "localhost",
        "PORT": 5432,
    }
}
```

Django's sync code (the ORM's sync methods, migrations, the admin, the test
runner) runs on psycopg, exactly as with Django's own Postgres backend.
djox's async code (`djox.Manager`, `djox.connections[alias]`) runs on a pool
of connections that Rust owns (`Database.postgres()`). Both are clients of
the same server, which is safe: unlike SQLite, Postgres is a server built
for many clients. So psycopg must be installed, as for Django's backend.

djox subclasses Django's classes and changes only what djox needs:

| File            | What it does                                                    |
|-----------------|-----------------------------------------------------------------|
| `base.py`       | `DatabaseWrapper`: hooks, noting transactions, opening the async database |
| `errors.py`     | Turns djox's exceptions into Django's, next to psycopg's          |
| `options.py`    | Reads djox's options and the connection settings the async database uses |
| `time_zones.py` | Moves async rows' dates and times into the connection's time zone |
| `creation.py`   | Closes the async database before Django drops a test database     |
| `operations.py` | Points Django at djox's compilers                                 |
| `compiler.py`   | Django's compilers, able to hand a statement to async code        |
"""
