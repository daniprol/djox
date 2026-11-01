//! The errors of djox.

use std::fmt;

/// Something that went wrong while talking to the database.
///
/// Each backend sorts its own driver's errors into these, so Python code
/// catches the same exceptions whatever the database is.
#[derive(Debug, Clone, PartialEq)]
pub enum Error {
    /// A constraint failed: a duplicate unique value, a missing foreign key row,
    /// a `NOT NULL` column left empty... Python raises `djox.IntegrityError`.
    Integrity(String),
    /// The database could not run the statement: a syntax error, a missing
    /// table, a lock that did not free up in time... Python raises `djox.OperationalError`.
    Operational(String),
    /// The caller's mistake: a wrong number of parameters, a statement that
    /// returns rows sent to `execute()`... Python raises `djox.ProgrammingError`.
    Programming(String),
    /// A value doesn't fit: a number out of range, a date that can't be
    /// read... Python raises `djox.DataError`.
    Data(String),
    /// The database's own state is wrong for the statement, like a failed
    /// transaction that must be rolled back. Python raises `djox.InternalError`.
    Internal(String),
    /// The database doesn't support what the statement asks. Python raises `djox.NotSupportedError`.
    NotSupported(String),
    /// Any other database error. Python raises `djox.DatabaseError`.
    Database(String),
    /// The settings can't work, like an in-memory SQLite database. Python raises `ValueError`.
    Settings(String),
    /// The database was closed.
    Closed,
}

/// The result of an operation that can fail: either `Ok(value)` or `Err(Error)`.
///
/// Rust has no exceptions. A function that can fail returns a `Result`, and the
/// `?` operator means "if this is an error, return it right away" (like letting
/// an exception propagate).
pub type Result<T> = std::result::Result<T, Error>;

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Integrity(message)
            | Error::Operational(message)
            | Error::Programming(message)
            | Error::Data(message)
            | Error::Internal(message)
            | Error::NotSupported(message)
            | Error::Database(message) => {
                write!(formatter, "{message}")
            }
            Error::Settings(message) => write!(formatter, "invalid database settings: {message}"),
            Error::Closed => write!(formatter, "the database is closed"),
        }
    }
}

impl std::error::Error for Error {}
