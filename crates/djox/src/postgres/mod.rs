//! The Postgres backend.
//!
//! # Why it looks like this
//!
//! Postgres is a server: a connection is a network socket, and many
//! connections can read and write at the same time (Postgres locks rows, not
//! the whole database). So, unlike SQLite, there is no single writer. The
//! backend keeps a *pool* of connections, and each statement runs on
//! whichever one is free.
//!
//! Waiting for the network is what async code is made for. The connections
//! run on a small `tokio` runtime that the database owns: a couple of
//! threads that run async Rust, the way an asyncio event loop runs
//! coroutines. Python sends a statement, the runtime runs it, and the answer
//! comes back to Python's event loop (see `ReplySender`).
//!
//! ```text
//!   Python (asyncio)          Rust (tokio runtime, 2 threads)
//!   ─────────────────         ──────────────────────────────────────────
//!   await fetch_all(...) ─►  task: borrow a connection ─► run ─┐
//!   await execute(...)   ─►  task: borrow a connection ─► run ─┼─► Postgres server
//!   await execute(...)   ─►  task: borrow a connection ─► run ─┘
//! ```
//!
//! Each connection prepares a statement the first time it sees its SQL and
//! keeps it, so later runs of the same SQL cost one round trip.
//!
//! Django's sync ORM keeps talking to the same server through psycopg, as
//! with Django's own Postgres backend. That is safe: two drivers on two
//! sockets are just two clients of the server (SQLite, a library inside the
//! process, needed djox's own sync connection instead).
//!
//! # Values
//!
//! | Postgres type                          | `Value`       | Python      |
//! |----------------------------------------|---------------|-------------|
//! | `boolean`                              | `Boolean`     | `bool`      |
//! | `smallint`, `integer`, `bigint`, `oid` | `Integer`     | `int`       |
//! | `real`, `double precision`             | `Real`        | `float`     |
//! | `numeric`                              | `Decimal`     | `Decimal`   |
//! | `text`, `varchar`, `char`, `citext`, `json`, `jsonb`, `inet`, `cidr` | `Text` | `str` |
//! | `bytea`                                | `Blob`        | `bytes`     |
//! | `date`, `time`, `timestamp`            | `Date`, `Time`, `DateTime` | `date`, `time`, naive `datetime` |
//! | `timestamptz`                          | `DateTimeUtc` | aware `datetime` (UTC) |
//! | `interval`                             | `Duration`    | `timedelta` |
//! | `uuid`                                 | `Uuid`        | `uuid.UUID` |
//! | `void` (what `SELECT pg_sleep(1)` returns) | `Null`     | `None`      |
//!
//! These are the objects psycopg gives Django's Postgres backend (Django
//! asks it for JSON and IP addresses as text), so Django sees the same
//! values from both.
//!
//! | File              | What it does                                                    |
//! |-------------------|-----------------------------------------------------------------|
//! | `database.rs`     | [`PostgresDatabase`]: the runtime, the pool, running statements, closing |
//! | `stream.rs`       | Streams: a query's rows a chunk at a time, on a connection of its own |
//! | `values.rs`       | djox values to parameters, and columns to djox values            |
//! | `errors.rs`       | Postgres errors (SQLSTATE codes) to djox errors                  |
//! | `settings.rs`     | [`PostgresSettings`]: the server, the pool size, the time zone   |
//! | `placeholders.rs` | Turns Django's `%s` placeholders into Postgres's `$1`, `$2`...    |

mod database;
mod errors;
mod placeholders;
mod settings;
mod stream;
mod values;

pub use database::PostgresDatabase;
pub use placeholders::to_numbered_placeholders;
pub use settings::PostgresSettings;
