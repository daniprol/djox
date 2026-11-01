# Notes for coding agents

djox is a truly async Django ORM with a Rust core. Django keeps doing what it
does well (models, migrations, admin, the QuerySet language); djox replaces the
part that talks to the database, so `await Book.objects.aget(...)` really runs
without blocking. SQLite first; Postgres later. Read this file before changing
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
| `crates/djox`       | The backends, one folder each: `sqlite/` today, `postgres/` later   |
| `src/*.rs`          | The Python extension `djox._core` (PyO3): the `Database` class, value conversions, exceptions |
| `src/djox`          | The Python package                                                  |
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
  return a `Reply`); statements sent in order run in order; callers never see
  "database is locked" under load.
- **The Python layer only knows the trait.** `djox.Database` holds an
  `Arc<dyn Database>`; each backend adds one constructor
  (`Database.sqlite(...)`, later `Database.postgres(...)`).
- **SQLite** (`crates/djox/src/sqlite`): one writer thread with its own queue,
  a few reader threads sharing another queue, WAL mode. See the module docs.
- **Rust does the heavy, repetitive work** (running SQL, converting rows,
  later compiling common queries). Python keeps the Django-facing API small
  and readable.

### Things to know

- **One copy of SQLite per process.** djox bundles its own SQLite and Python's
  `sqlite3` module has another. Two copies can't see each other's locks, so
  they must never open the same file in one process (writes get lost). That
  is why djox will ship a Django `ENGINE` for SQLite: Django's sync ORM then
  uses djox's copy too.
- **Django's SQLite SQL calls Python functions** (`django_date_extract`,
  `django_format_dtdelta`, `REGEXP`...) that Django registers on each
  connection. djox connections must register them before running SQL from
  Django's compiler for those lookups.
- **Django's compiler internals change between releases** (Django 6.1 added a
  `fetch_mode` argument to `from_db()`). Test against every supported Django.

## Roadmap

0. Benchmarks (done: `benchmarks/`).
1. The Rust SQLite engine and `djox.Database` (done).
2. djox as Django's SQLite `ENGINE`, with Django's SQL functions registered.
3. `djox.Manager`: Django's QuerySet `a`-methods running on djox. Anything not
   supported yet falls back to Django's own method, so nothing breaks.
4. Async `atomic()`, savepoints, `on_commit`.
5. Hooks, in the style of pyox-agents: plain functions (sync or async) called
   at fixed points (`before_query`, `after_query`...), returning `None` to
   change nothing or a new value to replace it. Django's signals keep working
   (sent with `Signal.asend()`).
6. The fast lane: Rust compiles common queries itself, cached per query shape.

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
| End to end      | `tests/e2e/`                  | Django models end to end (`tests/e2e/library` is a small Django app) |
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
  `@pytest.mark.django_adapted("why")` if it had to change, or
  `@pytest.mark.django_unsupported("why")` if djox can't pass it yet (it then
  runs as a strict `xfail`, so we notice when it starts passing). pytest ends
  with a count of unchanged, adapted and unsupported Django tests.
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
- The version lives in one place: `[workspace.package]` in `Cargo.toml`.
