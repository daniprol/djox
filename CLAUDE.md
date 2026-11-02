# Notes for coding agents

djox is a truly async Django ORM with a Rust core. Django keeps doing what it
does well (models, migrations, admin, the QuerySet language); djox replaces the
part that talks to the database, so `await Book.objects.aget(...)` really runs
without blocking. SQLite and Postgres. Read this file before changing
anything.

## The main rule: code that teaches

Everyone who reads this code is a Python developer. Many have never seen Rust,
and many have never looked inside an ORM. The code must be easy to read for
them, and it should teach them both along the way.

- **Clean and readable beats short and clever.** A few repeated lines are fine
  when they keep each piece simple. Don't add generics, macros, traits,
  lifetimes or abstractions to save lines. Don't build for needs we don't have
  yet.
- **Names say what things are, in plain words**: `reader_count`, `parameters`,
  `rows_affected`, `busy_timeout`, `statement_writes`. No abbreviations
  (`conn`, `stmt`, `cfg`, `ctx`, `idx`, `db`) and no Rust jargon in names.
- **Straight-line code.** Prefer a `for` loop and an `if` to a long iterator
  chain. Prefer a plain `match` to a clever helper.
- **Explain Rust as you go.** The first time a file uses a Rust feature a
  Python developer wouldn't know, add a short comment, ideally comparing it to
  Python: `Option` ("`T` or `None`"), `Result` and `?` ("return the error
  now", like letting an exception propagate), `&` (a borrowed reference),
  `move`, `Arc` (a shared pointer, like a Python reference), `Mutex` (like
  `threading.Lock`), traits (like a `Protocol`), `impl Trait for Type`,
  `#[derive(...)]`, `pub(crate)`, channels (like `queue.Queue`).
- **Explain the ORM as you go.** When code exists because of how databases or
  Django work (WAL mode, one SQLite writer, `%s` placeholders, `BEGIN
  IMMEDIATE`, why rows are tuples), say why in a comment.
- **Comments explain why, not what.** Every file starts with a `//!` (Rust) or
  module docstring (Python) saying what it does; folders list their files in a
  small table (see `crates/djox/src/sqlite/mod.rs`). Every public item has a
  `///` comment or docstring.
- **One job per file.** If a file does two things, split it.
- **Same words everywhere.** Rust, Python and the docs use the same names for
  the same things (`fetch_all`, `execute`, `parameters`, `reader`, `writer`,
  `backend`). Follow Django's names when Django has one (`atomic`, `aget`,
  `DATABASES`, `alias`, `IntegrityError`).
- The same rules apply to Python: small functions, clear names, type hints, no
  magic.

## Architecture

| Path                | What it is                                                         |
|---------------------|--------------------------------------------------------------------|
| `crates/djox-core`  | The shared types (`Value`, `Rows`, `Executed`, `Reply`, `Error`) and the `Database` trait. Small and stable |
| `crates/djox`       | The backends, one folder each: `sqlite/` and `postgres/`            |
| `src/*.rs`          | The Python extension `djox._core` (PyO3): `Database` and `RowStream` (async), `Connection` and `Cursor` (sync, for Django), SQL functions, value conversions, `ResultBuilder` (the fast lane's results), exceptions |
| `src/djox`          | The Python package, with Django's `ENGINE` in `backends/sqlite3/` and `djox.connections` |
| `tests`             | Python tests: `unit/`, `integration/`, `e2e/`, `django_suite/` (see Testing) |
| `benchmarks`        | Speed against Django's ORM and plain `sqlite3` (its own uv project)  |
| `docs`              | The documentation site (Zensical, `zensical.toml`)                  |

- **Backends are separate on purpose.** Each backend owns everything about its
  database (settings, connections, threads or pools, value conversions,
  placeholders, error mapping) in its own folder. They share only the
  `Database` trait and the types in `djox-core`. Some code will look alike in
  two backends; that is accepted, so each stays easy to read and to change.
- **The contract of a backend** (`crates/djox-core/src/database.rs`): SQL uses
  Django's `%s` placeholders; methods never block (they send the statement and
  return a `Reply`); a statement sent after another one's answer sees its
  writes; callers never see "database is locked" under load.
- **The Python layer only knows the trait.** `djox.Database` holds an
  `Arc<dyn Database>`; each backend adds one constructor
  (`Database.sqlite(...)`, `Database.postgres(...)`).
- **SQLite** (`crates/djox/src/sqlite`): one writer thread with its own queue,
  a few reader threads sharing another queue, WAL mode. See the module docs.
- **Postgres** (`crates/djox/src/postgres`): a pool of connections
  (`deadpool-postgres` on `tokio-postgres`) on a small tokio runtime the
  database owns; statements prepared once per connection; streams on a
  connection of their own. Its `ENGINE` (`src/djox/backends/postgresql`) is
  Django's Postgres backend, still on psycopg for sync code; only async code
  runs on djox's pool. Two drivers are two clients of the server, so (unlike
  SQLite) they can't lose each other's writes. Rust returns typed values
  (`Value::Decimal`, `DateTimeUtc`...) so Python gets what psycopg gives Django.
- **Django's `ENGINE`** (`src/djox/backends/sqlite3`): Django's own SQLite
  backend classes, subclassed, changing only what touches SQLite. Each Django
  thread gets a `djox.Connection` (sqlite3-shaped, runs on the caller's
  thread); `djox.connections[alias]` is the async `Database` for the same
  settings. Both are prepared by `prepare_connection()`.
- **`djox.Manager`** (`src/djox/query.py`): Django's `QuerySet`, with its
  `a`-methods running on djox (the "safe lane": Django writes the SQL, djox
  runs it). Rows are turned into results by Django's own iterable classes
  (`_results.py`); single-statement methods are replayed with the answer
  (`backends/sqlite3/compiler.py`); `_running.py` decides where the SQL runs.
- **The fast lane** (`src/djox/_fast_lane`): while a `djox.QuerySet` is only
  filtered with plain values (`exact`, `gt`, `in`, `isnull`...), ordered by
  columns and sliced, it keeps a note (`SimpleQuery`) instead of building
  Django's `Query`. Its methods, sync and async, reads and simple writes
  (`update()`, `delete()`), reuse the SQL Django wrote once for the same
  query *shape*, and Rust builds the results (`src/results.rs`: instances,
  dicts for `values()`, tuples for `values_list()`), converting dates,
  decimals and booleans like Django's converters (`src/conversions.rs`).
  Asking for `queryset.query` builds Django's `Query` from the note, so
  everything else falls back to Django. Django writes the SQL on purpose: it
  is written once per shape, so a second SQL writer in Rust would buy no
  speed.
- **Streams** (`aiterator()`): `Database.stream_rows()` reads a query on a
  connection and thread of its own, a chunk at a time, as Python asks
  (`crates/djox-core/src/stream.rs`).
- **Hooks** (`src/djox/hooks.py`): `djox.add_hooks()` adds plain functions
  run before and after every statement. Sync code is hooked through Django's
  `execute_wrappers`, async code in `_running.py`. Hooks are sync functions
  on purpose: they also run inside sync queries.
- **Related objects**: `select_related()` is part of a fast lane shape, and
  `ResultBuilder` builds the related objects from Django's `klass_info`
  (`RelatedModel` in `src/results.rs`). `prefetch_related()` keeps the note;
  Django's own prefetching runs unchanged on a djox thread
  (`src/djox/prefetch.py` says why it isn't awaited query by query).
- **djox's threads** (`src/djox/_threads.py`): Django's sync methods that
  djox doesn't split (`get_or_create()`, `bulk_create()`, `Model.save()` on a
  `djox.Model`...) run unchanged on a small pool of djox's threads, not on
  Django's single sync thread. Each pooled thread runs Django's
  `close_old_connections()` once per request, so `CONN_MAX_AGE` works.
  Related managers (`author.books.aadd()`) run there too (`related.py`).
- **Write turns** (`src/djox/_write_turns.py`): SQLite has one writer, so
  djox's plain writes (its writer thread) and transactions (a Django
  connection each) take turns in asyncio, like a readers-writer lock, instead
  of SQLite's sleep-and-retry.
- **Existing models** (`src/djox/install.py`, `apps.py`): with `"djox"` in
  `INSTALLED_APPS`, the models `DJOX_MODELS` chooses get djox managers and
  model methods when Django is ready. Their code and migrations don't change.
- **`djox.atomic()`** (`src/djox/transaction.py`): Django's own `atomic()`,
  run on a pooled thread that the block owns (asgiref's
  `ThreadSensitiveContext` sends the block's `sync_to_async()` calls there).
- **Rust does the heavy, repetitive work** (running SQL, converting rows,
  building model instances). Python keeps the Django-facing API small and
  readable.

### Things to know

- **One copy of SQLite per process.** djox bundles its own SQLite and Python's
  `sqlite3` module has another. Two copies can't see each other's locks, so
  they must never open the same file in one process (writes get lost). That
  is why Django must use djox's `ENGINE`: its sync ORM then uses djox's copy.
  Never open a database file with Python's `sqlite3`, in code or in tests.
- **Django's SQLite SQL calls Python functions** (`django_date_extract`,
  `django_format_dtdelta`, `REGEXP`...) that Django registers on each
  connection. `prepare_connection()` calls Django's own `register()` on every
  djox connection, sync and async, so they always match the installed Django.
- **Release the GIL before taking a connection's lock.** A thread holding the
  GIL and waiting for a connection, while the connection's thread runs a
  Python SQL function (which needs the GIL), would wait forever. Every
  `djox.Connection` method does `py.detach(...)` before `lock()`.
- **Rust conversions must match Django's exactly.** A column gets one only
  when its converter is Django's own, unchanged; any value Rust doesn't
  handle goes to Django's converter. `tests/unit/test_conversions.py`
  compares them value by value, errors included.
- **The fast lane must give Django's answer, or step aside.** Each value is
  prepared by its field when `filter()` runs, so errors come at the same
  moment as in Django. A new shape's statement is kept only if the fast lane
  computes the same parameters as Django did (value by value, type by type).
  Anything a shape can't capture (integers too big for the column, an empty
  `in`) is checked on every query.
- **Replay only side-effect-free methods.** `compiler.replay()` runs a Django
  method again once its statement is answered. A method that sends signals or
  computes defaults (`save()`) must use `compiler.capture()` instead, or it
  would do them twice.
- **Async code inside a sync transaction must run in it.** djox's
  `DatabaseWrapper.set_autocommit()` notes the open transaction in a context
  variable; `async_to_sync()` copies it to the async code, which then runs its
  SQL on that connection (`_running.open_sync_transaction()`).
- **A transaction lives on one thread.** Django keeps one connection per
  thread, so `djox.atomic()` must run Django's `atomic()`, the queries and
  the commit on the block's thread (or the transaction's connection). A task
  started with a fresh context inside a block isn't in it: its own block on
  the same file waits for the first one to end.
- **Django's compiler internals change between releases** (Django 6.1 added a
  `fetch_mode` argument to `from_db()`). Test against every supported Django.

## Roadmap

0. Benchmarks (done: `benchmarks/`).
1. The Rust SQLite engine and `djox.Database` (done).
2. djox as Django's SQLite `ENGINE`, with Django's SQL functions registered (done).
3. `djox.Manager`: Django's QuerySet `a`-methods running on djox (done).
   Anything not supported yet falls back to Django's own method, so nothing
   breaks. A query inside a sync `atomic()` block (like Django's `TestCase`)
   runs on that block's connection, or it couldn't see the block's writes.
4. `djox.atomic()` and `djox.on_commit()`: async transactions and savepoints
   (done). The methods that need a transaction, and `djox.Model`'s
   `asave()`, run Django's sync code on djox's own threads (done).
5. Hooks, in the style of pyox-agents (done): plain functions called before
   and after every statement (`before_query`, `after_query`), returning
   `None` to change nothing or a new value to replace it. Django's signals
   keep working (sent with `Signal.asend()`).
6. The fast lane (done): simple queries skip Django's query building and
   compiler, reusing Django's SQL per query shape, and Rust builds the
   results. It covers sync and async methods, `update()`, `delete()`,
   `values()` and `values_list()`, with dates, decimals and booleans
   converted in Rust. `aiterator()` streams its rows (done). Next: `F()` in
   `update()`, joins, `annotate()`.
7. Postgres (done): `Database.postgres()` and `djox.backends.postgresql`.
   Next: TLS, reading arrays, ranges and `hstore` from async code.
8. Related objects (done): `select_related()` on the fast lane (Rust builds
   the related objects and calls Django's setters to cache them), and
   `prefetch_related()` with its main query on the fast lane and Django's
   own prefetching, both on a djox thread (`src/djox/prefetch.py`).

Add features when someone needs them. If a change makes the core bigger, ask
whether it could be a hook instead.

## Testing

Design tests as carefully as the code. Each test checks one behavior, has a
name that says which (`test_execute_many_keeps_all_or_nothing`), and reads
like an example of how to use djox.

| Kind            | Where                         | What it checks                                         |
|-----------------|-------------------------------|--------------------------------------------------------|
| Rust unit       | `#[cfg(test)]` next to the code | One function or type: placeholders, value mapping, error sorting, queues |
| Python unit     | `tests/unit/`                 | One rule of the Python API: value conversions, exception classes |
| Integration     | `tests/integration/`          | `djox.Database` on real SQLite files: statements, concurrency, closing, settings |
| End to end      | `tests/e2e/`                  | Django end to end on djox's `ENGINE` (`tests/e2e/library` is a small Django app) |
| Django's own    | `tests/django_suite/`         | Django's ORM tests, ported (see below)                 |

- **Real files, no mocks.** Tests use real SQLite files in temporary folders
  and a real Django app. Mock only what can't be made real.
- **Test behavior, not internals**, so the code can be refactored freely.
- **Concurrency is a feature**: test it (many tasks at once, a slow query that
  must not block the event loop, closing while work is queued).
- **Every bug fix comes with the test that would have caught it.**
- **Ported Django tests** (`tests/django_suite/README.md`): as djox takes over
  a part of the ORM, copy Django's tests for it. Mark each with
  `@pytest.mark.django_ported("<django test id>")`, plus
  `@pytest.mark.django_adapted("why")` if it had to change (with a `# djox:` comment on each changed line), or
  `@pytest.mark.django_unsupported("why")` if djox can't pass it yet (it then
  runs as a strict `xfail`, so we notice when it starts passing). pytest ends
  with a count of unchanged, adapted and unsupported Django tests. Ported
  files stay as Django wrote them (no ruff, mypy or pyrefly on them), and run
  only on the Django version they were copied from.
- **Postgres too.** With `DJOX_POSTGRES_URL` set (a server's URL), the
  Rust and Python Postgres tests run; `DJOX_TEST_DATABASE=postgres` also
  runs the Django tests (djox's and the ported ones) on
  `djox.backends.postgresql`. Mark a test of what only the SQLite backend
  does `@pytest.mark.sqlite_only("why")`. CI runs both.
- **Every supported Django version** runs in CI: Django's compiler internals
  change between releases.

## Workflow

- Tools: uv, maturin, ruff, mypy, pyrefly, pytest, cargo fmt, clippy. Hooks
  run with prek (`prek.toml`): `uv run prek install` once.
- Types: all Python code, tests included, passes `mypy` (strict, with the
  django-stubs plugin) and `pyrefly check`. Both read Django's types from
  django-stubs. Fix the code rather than silencing a checker; when an ignore is
  truly needed (a test that passes a wrong type on purpose), use
  `# type: ignore[code]` with a short reason, which both checkers understand.
- `uv run pytest` builds the extension and runs the Python tests.
  `scripts/cargo-test.sh --workspace` runs the Rust tests.
- Docs: `uv run --only-group docs zensical serve`. Update them with every
  user-facing change.
- Docs pages are short and progressive: bullets over prose, one topic per
  page, the most common case first. Add a new page to `nav` in
  `zensical.toml`; the README links to the published site
  (`https://daniprol.github.io/djox/`), so it also works on PyPI.
- The version lives in one place: `[workspace.package]` in `Cargo.toml`.
  Note each release's changes in `CHANGELOG.md`.
- Releases (`.github/workflows/release.yml`): every push to main publishes a
  development build to TestPyPI (`0.1.0.dev42`, stamped by
  `scripts/stamp-dev-version.py`). To publish to PyPI, bump the version with
  `cargo set-version --bump patch` (or `minor`, `alpha`...; from
  `cargo install cargo-edit`), merge to main, then push the tag in Python form
  (`git tag v0.1.1 && git push origin v0.1.1`).
