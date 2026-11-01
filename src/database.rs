//! `djox.Database`: the Python class around a backend's [`djox::Database`].
//!
//! The class holds the backend behind the `Database` trait, so the same
//! Python class (and every method on it) works for every backend. Each
//! backend only adds a constructor: `Database.sqlite(...)` today,
//! `Database.postgres(...)` later.
//!
//! Each method sends its statement to the backend right away and returns an
//! `asyncio.Future` that the backend finishes when the answer arrives (see
//! `answers.rs`), so Python's event loop is never blocked. A Future rather
//! than a coroutine: the statement is already on its way before anyone awaits
//! it, so statements reach the database in the order the methods were called.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use djox::sqlite::{SqliteDatabase, SqliteSettings};
use djox::{Database, Rows, Value};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;

use crate::answers::future_for_answer;
use crate::errors::to_python_error;
use crate::values::{parameters_from_python, row_to_python, rows_to_python};

/// A database used from async code. Make one with a backend's constructor,
/// like `Database.sqlite("app.sqlite3")`.
///
/// `frozen` means Python can't set attributes on it, which lets many threads
/// use it at once without extra locking.
#[pyclass(name = "Database", module = "djox", frozen)]
pub struct PythonDatabase {
    /// The backend. `Arc` is a shared pointer (like a Python reference), and
    /// `dyn Database` means "any type that implements the `Database` trait".
    backend: Arc<dyn Database>,
    /// Where the database is, for `repr()`: a file path, later a server address.
    location: String,
}

#[pymethods]
impl PythonDatabase {
    /// Opens a SQLite database file (it is created if it doesn't exist).
    ///
    /// `#[staticmethod]` is Python's `@staticmethod`. `#[pyo3(signature = ...)]`
    /// writes the Python signature, defaults and keyword-only `*` included.
    #[staticmethod]
    #[pyo3(signature = (path, *, readers = None, timeout = 5.0))]
    fn sqlite(py: Python<'_>, path: PathBuf, readers: Option<usize>, timeout: f64) -> PyResult<Self> {
        if !(timeout.is_finite() && timeout >= 0.0) {
            return Err(PyValueError::new_err("timeout must be a number of seconds, 0 or more"));
        }
        let mut settings = SqliteSettings::new(path.clone());
        settings.busy_timeout = Duration::from_secs_f64(timeout);
        if let Some(reader_count) = readers {
            settings.reader_count = reader_count;
        }
        // Opening touches the disk: `detach` lets other Python threads run meanwhile.
        let backend = py.detach(|| SqliteDatabase::open(settings)).map_err(to_python_error)?;
        Ok(PythonDatabase {
            backend: Arc::new(backend),
            location: path.display().to_string(),
        })
    }

    /// Runs a query and returns all its rows as a list of tuples.
    #[pyo3(signature = (sql, parameters = None))]
    fn fetch_all<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        parameters: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let parameters = parameters_from_python(parameters)?;
        // `|py, rows: Rows| ...` is a closure, Rust's `lambda`.
        let (future, reply) = future_for_answer(py, |py, rows: Rows| Ok(rows_to_python(py, rows)?.into_any()))?;
        self.backend.fetch_all(sql, parameters, reply);
        Ok(future)
    }

    /// Runs a query and returns its first row, or `None` if it has none.
    #[pyo3(signature = (sql, parameters = None))]
    fn fetch_one<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        parameters: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let parameters = parameters_from_python(parameters)?;
        let (future, reply) = future_for_answer(py, |py, rows: Rows| match rows.rows.into_iter().next() {
            Some(row) => Ok(row_to_python(py, row)?.into_any()),
            None => Ok(py.None().into_bound(py)),
        })?;
        self.backend.fetch_all(sql, parameters, reply);
        Ok(future)
    }

    /// Runs a statement that changes data, and returns an `Executed`.
    #[pyo3(signature = (sql, parameters = None))]
    fn execute<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        parameters: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let parameters = parameters_from_python(parameters)?;
        let (future, reply) = future_for_answer(py, executed_to_python)?;
        self.backend.execute(sql, parameters, reply);
        Ok(future)
    }

    /// Runs a statement once for each list of parameters, in one transaction:
    /// either all of them are kept or none is.
    fn execute_many<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        parameter_lists: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let mut lists: Vec<Vec<Value>> = Vec::new();
        for parameters in parameter_lists.try_iter()? {
            lists.push(parameters_from_python(Some(&parameters?))?);
        }
        let (future, reply) = future_for_answer(py, executed_to_python)?;
        self.backend.execute_many(sql, lists, reply);
        Ok(future)
    }

    /// Closes the database. Statements already sent still finish; new ones
    /// raise `DatabaseClosedError`. Closing twice does nothing.
    fn close(&self) {
        self.backend.close();
    }

    /// Whether `close()` was called.
    #[getter]
    fn closed(&self) -> bool {
        self.backend.is_closed()
    }

    /// The backend's short name, like `"sqlite"`.
    #[getter]
    fn backend(&self) -> &str {
        self.backend.backend_name()
    }

    /// `with Database.sqlite(...) as database:` closes it at the end of the block.
    fn __enter__(this: Py<Self>) -> Py<Self> {
        this
    }

    #[pyo3(signature = (*_exception_info))]
    fn __exit__(&self, _exception_info: &Bound<'_, PyTuple>) {
        self.backend.close();
    }

    fn __repr__(&self) -> String {
        let closed = if self.backend.is_closed() { ", closed" } else { "" };
        format!("Database.{}('{}'{closed})", self.backend.backend_name(), self.location)
    }
}

/// What a statement that changes data gives back.
///
/// `get_all` makes every field readable from Python (`executed.rows_affected`).
#[pyclass(module = "djox", frozen, get_all)]
#[derive(Debug, Clone, Copy)]
pub struct Executed {
    /// How many rows the statement inserted, changed or deleted.
    rows_affected: u64,
    /// The id of the last inserted row (SQLite's `rowid`).
    last_insert_id: i64,
}

#[pymethods]
impl Executed {
    fn __repr__(&self) -> String {
        format!(
            "Executed(rows_affected={}, last_insert_id={})",
            self.rows_affected, self.last_insert_id
        )
    }
}

impl From<djox::Executed> for Executed {
    fn from(executed: djox::Executed) -> Self {
        Executed {
            rows_affected: executed.rows_affected,
            last_insert_id: executed.last_insert_id,
        }
    }
}

fn executed_to_python(py: Python<'_>, executed: djox::Executed) -> PyResult<Bound<'_, PyAny>> {
    Ok(Bound::new(py, Executed::from(executed))?.into_any())
}
