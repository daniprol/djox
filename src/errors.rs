//! The exceptions users can catch in Python.
//!
//! Inside Rust, a failing function returns `Err(...)`. PyO3 turns a `PyErr`
//! into a raised Python exception when it crosses into Python.
//!
//! Hierarchy (all importable from `djox`), named like Python's `sqlite3` and
//! Django's `django.db` errors:
//!
//! ```text
//! DatabaseError
//! ├── IntegrityError        a constraint failed (unique, foreign key, not null...)
//! ├── OperationalError      SQLite could not run the statement (syntax, missing table, lock timeout...)
//! └── DatabaseClosedError   the database was closed
//! ```
//!
//! Invalid settings (like an in-memory database) raise `ValueError`.

use djox::Error;
use pyo3::create_exception;
use pyo3::exceptions::{PyException, PyValueError};
use pyo3::prelude::*;

create_exception!(
    djox,
    DatabaseError,
    PyException,
    "Base class for all djox database errors."
);
create_exception!(
    djox,
    IntegrityError,
    DatabaseError,
    "A constraint failed: a duplicate unique value, a missing foreign key row, an empty NOT NULL column..."
);
create_exception!(
    djox,
    OperationalError,
    DatabaseError,
    "The database could not run the statement: a syntax error, a missing table, a lock that did not free up..."
);
create_exception!(djox, DatabaseClosedError, DatabaseError, "The database was closed.");

/// The Python exception for an engine error.
pub fn to_python_error(error: Error) -> PyErr {
    let message = error.to_string();
    match error {
        Error::Integrity(_) => IntegrityError::new_err(message),
        Error::Operational(_) => OperationalError::new_err(message),
        Error::Closed => DatabaseClosedError::new_err(message),
        Error::Settings(_) => PyValueError::new_err(message),
        Error::Database(_) => DatabaseError::new_err(message),
    }
}

pub fn add_exceptions_to_module(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = module.py();
    module.add("DatabaseError", py.get_type::<DatabaseError>())?;
    module.add("IntegrityError", py.get_type::<IntegrityError>())?;
    module.add("OperationalError", py.get_type::<OperationalError>())?;
    module.add("DatabaseClosedError", py.get_type::<DatabaseClosedError>())?;
    Ok(())
}
