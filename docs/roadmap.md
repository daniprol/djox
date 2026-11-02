# Roadmap

djox grows in small steps, each useful on its own. Everything below is in
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

## Next

- **Postgres**: reading arrays, ranges and `hstore` from async code.
- **More of the fast lane**: `F()` in `update()`, joins in `filter()`,
  `annotate()`, the prefetching queries themselves.

Need something else? [Open an issue](https://github.com/daniprol/djox/issues).
