# Roadmap

djox grows in small steps. Each one is useful on its own.

| Step | What | Status |
|------|------|--------|
| 0 | Benchmarks against Django's ORM and plain `sqlite3` | done |
| 1 | The Rust SQLite engine and `djox.Database` | done |
| 2 | djox as Django's SQLite `ENGINE`: one copy of SQLite in the process, Django's SQL functions (`django_date_extract`...) registered on djox's connections | done |
| 3 | `djox.Manager`: Django's QuerySet `a`-methods (`aget`, `acount`, `async for`, `acreate`, `aupdate`, `adelete`...) running on djox. Anything not supported yet falls back to Django's own method. Queries inside a sync `atomic()` block (like Django's `TestCase`) run on that block's connection | done |
| 4 | Async `atomic()`, savepoints, `on_commit`. Then the methods that need a transaction run on djox too: `aget_or_create`, `aupdate_or_create`, `abulk_create`, cascading `adelete`, model `asave`/`adelete` | next |
| 5 | Hooks: plain functions (sync or async) called before and after queries; Django's signals keep working | |
| 6 | The fast lane: Rust compiles common queries itself, cached per query shape | |
| later | Postgres, `prefetch_related` | |
