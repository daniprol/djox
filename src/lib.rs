//! The Python extension of djox (`djox._core`).
//!
//! The backends (the `djox` crate) know nothing about Python. This crate turns
//! their methods into Python awaitables, and converts values and errors.
//!
//! | File          | What it does                                                   |
//! |---------------|----------------------------------------------------------------|
//! | `database.rs` | `djox.Database`: the Python class around a backend's `Database`  |
//! | `answers.rs`  | Finishing an `asyncio.Future` when a backend answers            |
//! | `values.rs`   | Python values to djox values and back; rows to lists of tuples  |
//! | `errors.rs`   | The Python exception classes (`djox.DatabaseError`...)          |

use pyo3::prelude::*;

mod answers;
mod database;
mod errors;
mod values;

/// The module Python imports as `djox._core`.
#[pymodule]
fn _core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<database::PythonDatabase>()?;
    module.add_class::<database::Executed>()?;
    errors::add_exceptions_to_module(module)?;
    Ok(())
}
