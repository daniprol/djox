# Roadmap

djox grows in small steps. Each one is useful on its own.

| Step | What | Status |
|------|------|--------|
| 0 | Benchmarks against Django's ORM and plain `sqlite3` | done |
| 1 | The Rust SQLite engine and `djox.Database` | done |
| 2 | djox as Django's SQLite `ENGINE`: one copy of SQLite in the process, Django's SQL functions (`django_date_extract`...) registered on djox's connections | done |
| 3 | `djox.Manager`: Django's QuerySet `a`-methods (`aget`, `acount`, `async for`, `acreate`, `aupdate`, `adelete`...) running on djox. Anything not supported yet falls back to Django's own method. Queries inside a sync `atomic()` block (like Django's `TestCase`) run on that block's connection | done |
| 4 | `djox.atomic()` (async transactions, savepoints, decorator) and `djox.on_commit()`, running Django's own `atomic()` on a thread of the block's own | done |
| 4b | The methods that need a transaction (`aget_or_create`, `aupdate_or_create`, `abulk_create`, cascading `adelete`, `prefetch_related`) and `djox.Model`'s `asave`/`adelete`/`arefresh_from_db` run Django's sync method on djox's own thread pool, with writers taking turns | done |
| 5 | Hooks: plain functions called before and after every statement (`djox.add_hooks()`), able to watch or change it; Django's signals keep working | done |
| 6 | The fast lane: simple reads (`aget`, `async for`, `acount`...) skip Django's query building and compiler, reusing the SQL Django wrote for the same query shape, and Rust builds the model instances | done |
| 6b | The fast lane for sync methods, for writes (`update`, `delete`), `values()` and `values_list()`; dates, times, decimals and booleans converted in Rust; `aiterator()` streams its rows | done |
| 7 | Postgres: `djox.Database.postgres()` (a pool of Rust connections) and the `djox.backends.postgresql` `ENGINE` (Django's backend on psycopg for sync code, djox for async code) | done |
| 8 | `select_related()` on the fast lane (Rust builds the related objects), and `prefetch_related()` with its main query on the fast lane and Django's prefetching, both on a djox thread; `djox.aprefetch_related_objects()` | done |
| later | TLS for Postgres, more of the fast lane (`F()` in `update()`, joins in `filter()`, `annotate()`, the prefetching queries themselves) | |
