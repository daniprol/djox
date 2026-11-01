"""Django's SQLite backend, running on the SQLite bundled in djox.

```python
DATABASES = {
    "default": {
        "ENGINE": "djox.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}
```

Django's own SQLite backend is a handful of classes: features, operations,
schema editor, introspection, creation and the `DatabaseWrapper` that opens
connections. They all write SQL and send it through a cursor. djox subclasses
them and changes only what touches SQLite itself, so everything else (the ORM,
migrations, the admin, the test runner) works as usual.

| File          | What it does                                                      |
|---------------|-------------------------------------------------------------------|
| `base.py`     | `DatabaseWrapper`: opens `djox.Connection`s and prepares them      |
| `options.py`  | Reads and checks `DATABASES[alias]["OPTIONS"]`                    |
| `dbapi.py`    | What Django looks up on a driver: exceptions and the SQLite version |
| `features.py` | Feature flags that depend on djox's SQLite version                |
| `creation.py` | Test databases: files instead of memory, so async code can share them |
| `operations.py` | Points Django at djox's compilers                               |
| `compiler.py` | Django's compilers, able to hand a statement to async code (`djox.QuerySet`) |
"""
