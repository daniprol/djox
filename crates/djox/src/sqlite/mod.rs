//! The SQLite backend.
//!
//! # Why it looks like this
//!
//! SQLite is a library, not a server: a "connection" is a handle to a file,
//! and every call on it *blocks* until SQLite is done. Async Python must never
//! block, so each connection lives on its own thread (a *worker*) and takes
//! statements from a queue. Python puts a statement in a queue and awaits the
//! reply; its event loop keeps running meanwhile.
//!
//! SQLite allows **one writer at a time** for the whole file. Two connections
//! that try to write at once make one of them wait, and then fail with
//! "database is locked". So there is exactly **one writer worker**, and every
//! write waits its turn in the writer's queue instead. Reads go to a few
//! **reader workers** that share another queue and run in parallel.
//!
//! In **WAL mode** (write-ahead log) readers never wait for the writer and the
//! writer never waits for readers: a reader sees the database as it was when
//! its statement started. That is what makes this layout fast.
//!
//! ```text
//!   Python (asyncio)          Rust
//!   ─────────────────         ──────────────────────────────────────────
//!   await execute(...)  ──►  writer queue  ──►  writer thread  ──┐
//!   await fetch_all(...) ─►  reader queue  ──►  reader thread 1 ─┼─► app.sqlite3
//!                                          ──►  reader thread 2 ─┘
//! ```
//!
//! | File              | What it does                                                    |
//! |-------------------|-----------------------------------------------------------------|
//! | `database.rs`     | [`SqliteDatabase`]: opens the connections, starts the workers, routes statements |
//! | `worker.rs`       | A worker: a thread that runs a queue on its connection           |
//! | `writer.rs`       | `SharedWriter`: the writer connection, shared by the writer thread, quick writes and transaction blocks |
//! | `readers.rs`      | `ReaderQueue`: the queue readers share, waking the reader that slept last |
//! | `quick_reads.rs`  | `QuickReader`: small reads run right on the calling thread, without the trip to a reader |
//! | `statement_speeds.rs` | `StatementSpeeds`: which statements were quick lately, and which ran out of time |
//! | `connection.rs`   | [`SqliteConnection`]: one connection, opening it, running statements, errors |
//! | `functions.rs`    | SQL functions written outside SQLite (Django registers ~40)      |
//! | `settings.rs`     | [`SqliteSettings`]: the file, the lock timeout, the number of readers |
//! | `placeholders.rs` | Turns Django's `%s` placeholders into SQLite's `?`               |

mod connection;
mod database;
mod functions;
mod placeholders;
mod quick_reads;
mod readers;
mod settings;
mod statement_speeds;
mod worker;
mod writer;

pub use connection::{Access, QuickOutcome, SqliteConnection, StatementOutput, is_data_change};
pub use database::SqliteDatabase;
pub use functions::{AggregateState, NewAggregateState};
pub use placeholders::{named_to_question_marks, to_question_marks};
pub use settings::SqliteSettings;
pub use writer::SharedWriter;

/// The version of the SQLite bundled in djox, like `"3.54.1"`.
pub fn sqlite_version() -> &'static str {
    rusqlite::version()
}
