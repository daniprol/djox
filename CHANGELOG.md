# Changelog

All notable changes to djox. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Before 1.0, a minor version may break things; each
break comes with a migration note.

## [Unreleased]

### Added

- **`djox.asgi`**: Django's ASGI handler, sending `request_started` and `request_finished` on the event loop
  when that can't block it, instead of hopping to a thread twice per request (an empty async view: 1.3 ms to
  0.16 ms).
- **Quick SQLite reads**: a small async read runs right on the calling thread, stopped after
  `quick_read_limit` (a new option, 1 ms) and sent to a reader then (`aget()`: 174 µs to 52 µs).
- **The fast lane takes more**: `Q()` objects, `exclude()`, and names through foreign keys and one-to-one
  fields (`author__name`), in filters and in `order_by()`.
- **New objects on the fast lane**: `create()`, `acreate()` and `djox.Model.asave()` of a new object reuse the
  `INSERT` Django wrote for the model's first one (`acreate()`: 502 µs to 269 µs).
- **TLS on Postgres**: djox's async connections are encrypted and checked like libpq's, with the same
  `OPTIONS` (`sslmode` from `disable` to `verify-full`, `sslrootcert`, `sslcert`, `sslkey`), also as arguments
  of `djox.Database.postgres()`. Built on rustls: nothing to install.

### Changed

- From async code, saving an object of a model with sync `pre_save` or `post_save` receivers runs on a djox
  thread, rather than sending each signal to Django's sync thread.
- Fewer lookups per query: the Python around each query is about half what it was.

### Fixed

- `acreate()` now calls a model's own `save()`.
- An `INSERT ... RETURNING` sent to `fetch_all()` goes straight to SQLite's writer thread, not through a
  reader.

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
- **`djox[postgres]`**: the extra that installs psycopg 3 for Django's sync code on Postgres (psycopg2 isn't
  supported). djox's async Postgres connections don't support TLS yet.
- Tested on Django 5.2, 6.0 and 6.1, Python 3.11 to 3.14, SQLite and Postgres 15+, with 2,395 of Django's own
  ORM tests ported (2,390 pass).

[0.1.0]: https://github.com/daniprol/djox/releases/tag/v0.1.0
