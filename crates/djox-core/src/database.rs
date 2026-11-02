//! [`Database`]: what every backend must be able to do.

use crate::{ChunkRequests, Executed, ReplySender, Rows, Value};

/// A database that runs SQL for async Python code without blocking it.
///
/// A *trait* is a list of methods a type promises to have, like a Python
/// `Protocol` or an abstract base class. The Python layer only knows this
/// trait, so it works the same with every backend: SQLite
/// (`djox::sqlite::SqliteDatabase`) and Postgres (`djox::postgres::PostgresDatabase`).
///
/// The rules every backend follows:
///
/// - **SQL uses Django's `%s` placeholders**, because that is what Django's
///   SQL compiler writes. Each backend converts them to its own style (`?` for
///   SQLite, `$1` for Postgres). `%%` is a literal `%`.
/// - **Methods never block** (but [`Database::fetch_all_quickly`], which may
///   for a moment, by design). Each one sends the statement right away and
///   returns; the answer comes later through the [`ReplySender`], exactly
///   once. A statement sent after an earlier one was answered sees what the
///   earlier one did. Statements sent at the same moment may run at the same
///   time (on SQLite's readers, or on two connections of a Postgres pool), so
///   wait for one before sending the next when their order matters.
/// - **Writes are never lost to a busy database.** The backend queues or pools
///   connections so callers don't see "database is locked" errors under load.
///
/// `Send + Sync` means one database can be used from many threads at once
/// (Python objects always can; Rust asks you to say so).
pub trait Database: Send + Sync {
    /// The backend's short name: `"sqlite"` or `"postgres"`.
    fn backend_name(&self) -> &str;

    /// Runs a query and returns all its rows.
    fn fetch_all(&self, sql: String, parameters: Vec<Value>, reply: ReplySender<Rows>);

    /// Runs a query right here, on the calling thread, if the backend can
    /// answer it within a moment: a *quick read*. Returns `None` when it
    /// can't (the statement writes, takes longer, or the backend has no quick
    /// reads); then send it with [`Database::fetch_all`] as usual.
    ///
    /// For a small query, sending it to another thread and waiting for the
    /// answer takes longer than running it. The calling thread waits for a
    /// quick read, but never longer than the backend's limit (the SQLite
    /// backend's `quick_read_limit`). The default answers `None`: Postgres
    /// has to wait for the network, so it has no quick reads.
    ///
    /// `&[Value]` borrows the parameters (a read-only view of a list), so they
    /// can still be sent to `fetch_all` afterwards.
    fn fetch_all_quickly(&self, _sql: &str, _parameters: &[Value]) -> Option<Rows> {
        None
    }

    /// Runs a query and hands its rows over `chunk_size` at a time, each
    /// chunk when the caller asks for it through its [`crate::RowStream`].
    ///
    /// The stream gets a connection of its own until it ends (or the caller
    /// drops its `RowStream`), so a slow caller never holds up other
    /// statements, and other statements, even ones sent while the stream is
    /// read, never wait for it. Only queries that read can be streamed.
    fn stream_rows(&self, sql: String, parameters: Vec<Value>, chunk_size: usize, requests: ChunkRequests);

    /// Runs a statement that changes data (`INSERT`, `UPDATE`, `DELETE`...).
    fn execute(&self, sql: String, parameters: Vec<Value>, reply: ReplySender<Executed>);

    /// Runs the same statement once for each list of parameters, in one
    /// transaction: either all of them are kept or none is.
    fn execute_many(&self, sql: String, parameter_lists: Vec<Vec<Value>>, reply: ReplySender<Executed>);

    /// Closes the database. Statements already sent still finish; new ones
    /// fail with [`crate::Error::Closed`]. Closing twice does nothing.
    fn close(&self);

    /// Whether [`Database::close`] was called.
    fn is_closed(&self) -> bool;
}
