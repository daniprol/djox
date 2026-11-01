//! The exceptions users can catch in Python.
//!
//! Inside Rust, a failing function returns `Err(...)`. PyO3 turns a `PyErr`
//! into a raised Python exception when it crosses into Python.
//!
//! The classes follow PEP 249, the standard for Python database drivers, like
//! Python's `sqlite3` and psycopg. Django looks up exactly these names on a
//! driver to turn its errors into `django.db.IntegrityError` and friends. All
//! are importable from `djox`:
//!
//! ```text
//! Error
//! ├── InterfaceError          a problem with djox itself, not the database
//! │   └── DatabaseClosedError the database or connection was closed
//! └── DatabaseError
//!     ├── DataError           a value doesn't fit (PEP 249; SQLite rarely raises it)
//!     ├── OperationalError    SQLite could not run the statement (missing table, lock timeout...)
//!     ├── IntegrityError      a constraint failed (unique, foreign key, not null...)
//!     ├── InternalError       SQLite's own state is broken (PEP 249; rare)
//!     ├── ProgrammingError    the caller's mistake (wrong number of parameters...)
//!     └── NotSupportedError   SQLite can't do this (PEP 249; rare)
//! ```
//!
//! Invalid settings (like an in-memory database for `Database.sqlite`) raise `ValueError`.

use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;

// `create_exception!` is a *macro* (code that writes code, marked by `!`):
// each line here defines a Python exception class, like `class X(Base): """doc"""`.
create_exception!(djox, Error, PyException, "Base class for all djox errors.");
create_exception!(
    djox,
    InterfaceError,
    Error,
    "A problem with djox itself rather than with the database."
);
create_exception!(
    djox,
    DatabaseClosedError,
    InterfaceError,
    "The database or connection was closed."
);
create_exception!(
    djox,
    DatabaseError,
    Error,
    "Base class for errors reported by the database."
);
create_exception!(djox, DataError, DatabaseError, "A value doesn't fit its column.");
create_exception!(
    djox,
    OperationalError,
    DatabaseError,
    "The database could not run the statement: a syntax error, a missing table, a lock that did not free up..."
);
create_exception!(
    djox,
    IntegrityError,
    DatabaseError,
    "A constraint failed: a duplicate unique value, a missing foreign key row, an empty NOT NULL column..."
);
create_exception!(
    djox,
    InternalError,
    DatabaseError,
    "The database's own state is broken."
);
create_exception!(
    djox,
    ProgrammingError,
    DatabaseError,
    "The caller's mistake: a wrong number of parameters, a statement that returns rows sent to execute()..."
);
create_exception!(
    djox,
    NotSupportedError,
    DatabaseError,
    "The database doesn't support this."
);

/// The Python exception for an engine error.
///
/// `djox::Error` is the engine's error type (a Rust `enum`); the `Error` above is the Python class.
pub fn to_python_error(error: djox::Error) -> PyErr {
    let message = error.to_string();
    match error {
        djox::Error::Integrity(_) => IntegrityError::new_err(message),
        djox::Error::Operational(_) => OperationalError::new_err(message),
        djox::Error::Programming(_) => ProgrammingError::new_err(message),
        djox::Error::Closed => DatabaseClosedError::new_err(message),
        djox::Error::Settings(_) => PyValueError::new_err(message),
        djox::Error::Database(_) => DatabaseError::new_err(message),
    }
}

pub fn add_exceptions_to_module(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = module.py();
    module.add("Error", py.get_type::<Error>())?;
    module.add("InterfaceError", py.get_type::<InterfaceError>())?;
    module.add("DatabaseClosedError", py.get_type::<DatabaseClosedError>())?;
    module.add("DatabaseError", py.get_type::<DatabaseError>())?;
    module.add("DataError", py.get_type::<DataError>())?;
    module.add("OperationalError", py.get_type::<OperationalError>())?;
    module.add("IntegrityError", py.get_type::<IntegrityError>())?;
    module.add("InternalError", py.get_type::<InternalError>())?;
    module.add("ProgrammingError", py.get_type::<ProgrammingError>())?;
    module.add("NotSupportedError", py.get_type::<NotSupportedError>())?;
    Ok(())
}
