# Roadmap

djox grows in small steps. Each one is useful on its own.

| Step | What | Status |
|------|------|--------|
| 0 | Benchmarks against Django's ORM and plain `sqlite3` | done |
| 1 | The Rust SQLite engine and `djox.Database` | done |
| 2 | djox as Django's SQLite `ENGINE`: one copy of SQLite in the process, Django's SQL functions (`django_date_extract`...) registered on djox's connections | next |
| 3 | `djox.Manager`: Django's QuerySet `a`-methods (`aget`, `acount`, `async for`, `acreate`...) running on djox. Anything not supported yet falls back to Django's own method | |
| 4 | Async `atomic()`, savepoints, `on_commit` | |
| 5 | Hooks: plain functions (sync or async) called before and after queries; Django's signals keep working | |
| 6 | The fast lane: Rust compiles common queries itself, cached per query shape | |
| later | Postgres, `prefetch_related` | |
