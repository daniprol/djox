# Roadmap

djox grows in small steps, each useful on its own. Steps 0 to 8 are in
0.1.0.

| Step | What |
|------|------|
| 0 | Benchmarks against Django's ORM and plain `sqlite3` |
| 1 | The Rust SQLite engine and `djox.Database` |
| 2 | djox as Django's SQLite `ENGINE`: one copy of SQLite in the process, Django's SQL functions registered |
| 3 | Django's QuerySet `a`-methods on djox, falling back to Django's own for the rest; queries inside a sync `atomic()` (like `TestCase`) |
| 4 | `djox.atomic()` and `djox.on_commit()`; methods that need a transaction (`aget_or_create()`, `abulk_create()`...) and `asave()` on djox's threads, with writers taking turns |
| 5 | Hooks: `before_query` and `after_query` around every statement |
| 6 | The fast lane: simple queries, sync and async, reads and writes, skip Django's compiler; Rust builds the results; streamed `aiterator()` |
| 7 | Postgres: `djox.Database.postgres()` and `djox.backends.postgresql` |
| 8 | Related objects: `select_related()` on the fast lane, `prefetch_related()` off the event loop |
| 9 | Speed (unreleased): `djox.asgi`, quick SQLite reads, `Q()`, `exclude()` and joins through foreign keys on the fast lane, `create()` and `asave()` too |
| 10 | The general fast lane (unreleased): any chain of calls is a recipe, so `annotate()`, `F()`, aggregates and joins through any relation take the fast lane |
| 11 | Several statements awaited (unreleased): `aget_or_create()`, `ain_bulk()`, `abulk_create()` and `abulk_update()` run on djox, not on a thread; `bulk_update()` and `in_bulk()` on the fast lane |

## Next

- **More of the fast lane**: subqueries (`Exists()`, `Subquery()`),
  prefetching through a foreign key or many-to-many.

Need something else? [Open an issue](https://github.com/daniprol/djox/issues).
