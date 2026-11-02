# Changelog

All notable changes to djox. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Before 1.0, a minor version may break things; each
break comes with a migration note.

## [0.1.0] - 2026-11-02

The first release.

### Added

- **Django `ENGINE`s**: `djox.backends.sqlite3` (one copy of SQLite in the process, WAL mode, Django's SQL
  functions registered) and `djox.backends.postgresql` (Django's backend for sync code, a Rust pool for async
  code).
- **Async QuerySets**: `aget()`, `acount()`, `async for`, `acreate()`, `aupdate()`, `adelete()`,
  `aget_or_create()`... run on djox's connections, not on Django's worker thread. Anything else falls back to
  Django's own method.
- **`DJOX_MODELS`**, `djox.Manager` and `djox.Model`: put every model, or the ones you choose, on djox, `User`
  included, without changing their code or migrations.
- **The fast lane**: simple queries, sync and async, reuse the SQL Django wrote once per query shape, and Rust
  builds the results (instances, `values()`, `values_list()`, `select_related()`).
- **Transactions**: `djox.atomic()` and `djox.on_commit()` for async code; SQLite writers take turns in asyncio
  instead of sleeping and retrying.
- **Streaming**: `aiterator()` reads a chunk at a time, on a connection of its own.
- **Related objects**: `select_related()` on the fast lane, `prefetch_related()` and
  `djox.aprefetch_related_objects()` off the event loop.
- **Hooks**: `djox.add_hooks()` calls plain functions before and after every statement.
- **Raw SQL**: `djox.Database.sqlite()` and `djox.Database.postgres()`, and `djox.connections[alias]`.
- Tested on Django 5.2, 6.0 and 6.1, Python 3.11 to 3.14, SQLite and Postgres 15+, with 2,395 of Django's own
  ORM tests ported (2,390 pass).

[0.1.0]: https://github.com/daniprol/djox/releases/tag/v0.1.0
