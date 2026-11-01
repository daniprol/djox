//! The Python extension of djox (`djox._core`).
//!
//! The backends (the `djox` crate) know nothing about Python. This crate turns
//! their methods into Python awaitables, and converts values and errors.
//!
//! | File          | What it does                                                   |
//! |---------------|----------------------------------------------------------------|
//! | `database.rs`   | `djox.Database`: the async class around a backend's `Database` |
//! | `answers.rs`    | Finishing an `asyncio.Future` when a backend answers           |
//! | `connection.rs` | `djox.Connection`: one SQLite connection for sync code (Django's ORM) |
//! | `cursor.rs`     | `djox.Cursor`: runs statements on a `Connection`, hands back rows |
//! | `functions.rs`  | Python functions and aggregates as SQL functions               |
//! | `threads.rs`    | Taking the GIL from djox's worker threads, quickly             |
//! | `values.rs`   | Python values to djox values and back; rows to lists of tuples  |
//! | `errors.rs`   | The Python exception classes (`djox.DatabaseError`...)          |

use pyo3::prelude::*;

mod answers;
mod connection;
mod cursor;
mod database;
mod errors;
mod functions;
mod threads;
mod values;

/// The module Python imports as `djox._core`.
#[pymodule]
fn _core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<database::PythonDatabase>()?;
    module.add_class::<database::Executed>()?;
    module.add_class::<connection::PythonConnection>()?;
    module.add_class::<cursor::Cursor>()?;
    // The version of the SQLite bundled in djox, like `"3.54.1"`.
    module.add("sqlite_version", djox::sqlite::sqlite_version())?;
    errors::add_exceptions_to_module(module)?;
    Ok(())
}
