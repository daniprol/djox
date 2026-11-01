//! `djox.Database`: the Python class around a backend's [`djox::Database`].
//!
//! The class holds the backend behind the `Database` trait, so the same
//! Python class (and every method on it) works for every backend. Each
//! backend only adds a constructor: `Database.sqlite(...)` and
//! `Database.postgres(...)`.
//!
//! Each method sends its statement to the backend right away and returns an
//! `asyncio.Future` that the backend finishes when the answer arrives (see
//! `answers.rs`), so Python's event loop is never blocked. A Future rather
//! than a coroutine: the statement is already on its way before anyone awaits
//! it, so statements reach the database in the order the methods were called.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use djox::postgres::{PostgresDatabase, PostgresSettings};
use djox::sqlite::{SqliteConnection, SqliteDatabase, SqliteSettings};
use djox::{Database, Rows, Value};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;

use crate::answers::{future_for_answer, future_for_stream_answer};
use crate::connection::PythonConnection;
use crate::errors::to_python_error;
use crate::values::{parameters_from_python, row_to_python, rows_to_python};

/// A database used from async code. Make one with a backend's constructor,
/// like `Database.sqlite("app.sqlite3")` or `Database.postgres("postgresql://localhost/app")`.
///
/// `frozen` means Python can't set attributes on it, which lets many threads
/// use it at once without extra locking.
#[pyclass(name = "Database", module = "djox", frozen)]
pub struct PythonDatabase {
    /// The backend. `Arc` is a shared pointer (like a Python reference), and
    /// `dyn Database` means "any type that implements the `Database` trait".
    backend: Arc<dyn Database>,
    /// Where the database is, for `repr()`: a file path, or a server's address and database name.
    location: String,
}

#[pymethods]
impl PythonDatabase {
    /// Opens a SQLite database file (it is created if it doesn't exist).
    ///
    /// `prepare`, if given, is called with each new connection (as a
    /// `djox.Connection`) before it starts working, to register SQL functions
    /// or run `PRAGMA`s. The connection is only lent: it can't be used after
    /// `prepare` returns.
    ///
    /// `#[staticmethod]` is Python's `@staticmethod`. `#[pyo3(signature = ...)]`
    /// writes the Python signature, defaults and keyword-only `*` included.
    #[staticmethod]
    #[pyo3(signature = (path, *, readers = None, timeout = 5.0, prepare = None))]
    fn sqlite(
        py: Python<'_>,
        path: PathBuf,
        readers: Option<usize>,
        timeout: f64,
        prepare: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        if !(timeout.is_finite() && timeout >= 0.0) {
            return Err(PyValueError::new_err("timeout must be a number of seconds, 0 or more"));
        }
        let mut settings = SqliteSettings::new(path.clone());
        settings.busy_timeout = Duration::from_secs_f64(timeout);
        if let Some(reader_count) = readers {
            settings.reader_count = reader_count;
        }
        let location = path.display().to_string();

        // An exception raised by `prepare` while opening is kept here and
        // raised as it is, instead of being turned into a djox error. The
        // closure below is kept by the database (streams open connections
        // later), so it owns what it uses: `move`, and an `Arc` (a shared
        // pointer) to the `Mutex` that stores the error.
        let prepare_error: Arc<Mutex<Option<PyErr>>> = Arc::new(Mutex::new(None));
        let kept_error = Arc::clone(&prepare_error);
        let connection_location = location.clone();
        let prepare_connection = move |sqlite: SqliteConnection| -> djox::Result<SqliteConnection> {
            let Some(prepare) = &prepare else {
                return Ok(sqlite);
            };
            Python::attach(|py| {
                let lent = Py::new(py, PythonConnection::lend(sqlite, connection_location.clone()))
                    .map_err(|error| keep_error(&kept_error, error))?;
                let called = prepare.call1(py, (lent.clone_ref(py),));
                // Take the connection back whatever happened, so it is closed properly.
                let sqlite = lent.get().take_back();
                called.map_err(|error| keep_error(&kept_error, error))?;
                sqlite.ok_or_else(|| djox::Error::Settings("prepare() closed the connection".to_string()))
            })
        };

        // Opening touches the disk: `detach` lets other Python threads run meanwhile.
        let opened = py.detach(|| SqliteDatabase::open_with(settings, prepare_connection));
        if let Some(error) = prepare_error.lock().unwrap().take() {
            return Err(error);
        }
        let backend = opened.map_err(to_python_error)?;
        Ok(PythonDatabase {
            backend: Arc::new(backend),
            location,
        })
    }

    /// Opens a pool of connections to a Postgres server.
    ///
    /// `connection` is a URL (`"postgresql://user:password@localhost:5432/app"`)
    /// or libpq's `key=value` words (`"host=localhost dbname=app user=django"`).
    /// `pool_size` is the most connections open at once. `time_zone` is
    /// the session's time zone (`SET TIME ZONE`), like Django sets it.
    ///
    /// It opens one connection right away, so wrong settings fail here.
    #[staticmethod]
    #[pyo3(signature = (connection, *, pool_size = 10, time_zone = None))]
    fn postgres(py: Python<'_>, connection: String, pool_size: usize, time_zone: Option<String>) -> PyResult<Self> {
        let mut settings = PostgresSettings::new(connection);
        settings.pool_size = pool_size;
        settings.time_zone = time_zone;
        let location = settings.describe();
        // Connecting waits for the network: `detach` lets other Python threads run meanwhile.
        let backend = py
            .detach(|| PostgresDatabase::open(settings))
            .map_err(to_python_error)?;
        Ok(PythonDatabase {
            backend: Arc::new(backend),
            location,
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

    /// Runs a query and hands its rows over a chunk at a time: a `RowStream`.
    ///
    /// Each `await stream.next_chunk()` gives up to `chunk_size` rows; fewer
    /// means the query has no more. The stream reads on a connection of its
    /// own, so other statements never wait for it. Only queries that read
    /// can be streamed.
    #[pyo3(signature = (sql, parameters = None, *, chunk_size = 2000))]
    fn stream_rows(
        &self,
        sql: String,
        parameters: Option<&Bound<'_, PyAny>>,
        chunk_size: usize,
    ) -> PyResult<RowStream> {
        if chunk_size == 0 {
            return Err(PyValueError::new_err("chunk_size must be 1 or more"));
        }
        let parameters = parameters_from_python(parameters)?;
        let (stream, requests) = djox::RowStream::new();
        self.backend.stream_rows(sql, parameters, chunk_size, requests);
        Ok(RowStream { stream, chunk_size })
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
    fn close(&self, py: Python<'_>) {
        // Closing waits for the statements already sent, whose answers need
        // the GIL: let go of it meanwhile.
        py.detach(|| self.backend.close());
    }

    /// Whether `close()` was called.
    #[getter]
    fn closed(&self) -> bool {
        self.backend.is_closed()
    }

    /// The backend's short name: `"sqlite"` or `"postgres"`.
    #[getter]
    fn backend(&self) -> &str {
        self.backend.backend_name()
    }

    /// `with Database.sqlite(...) as database:` closes it at the end of the block.
    fn __enter__(this: Py<Self>) -> Py<Self> {
        this
    }

    #[pyo3(signature = (*_exception_info))]
    fn __exit__(&self, py: Python<'_>, _exception_info: &Bound<'_, PyTuple>) {
        self.close(py);
    }

    fn __repr__(&self) -> String {
        let closed = if self.backend.is_closed() { ", closed" } else { "" };
        format!("Database.{}('{}'{closed})", self.backend.backend_name(), self.location)
    }
}

/// The rows of one query, a chunk at a time (from `Database.stream_rows()`).
///
/// ```python
/// stream = database.stream_rows("SELECT * FROM book", chunk_size=500)
/// while True:
///     rows = await stream.next_chunk()
///     ...
///     if len(rows) < stream.chunk_size:
///         break  # that was the last chunk
/// ```
///
/// When the stream is garbage collected, its connection is let go, even if
/// not every row was read.
#[pyclass(module = "djox", frozen)]
pub struct RowStream {
    stream: djox::RowStream,
    /// The most rows a chunk has.
    #[pyo3(get)]
    chunk_size: usize,
}

#[pymethods]
impl RowStream {
    /// The next chunk of rows, as a list of tuples. A chunk with fewer than
    /// `chunk_size` rows is the last one; asking again raises
    /// `DatabaseClosedError`.
    fn next_chunk<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        // The answer comes from the stream's own thread, which ends with the stream.
        let (future, reply) = future_for_stream_answer(py, |py, rows: Rows| Ok(rows_to_python(py, rows)?.into_any()))?;
        self.stream.next_chunk(reply);
        Ok(future)
    }

    fn __repr__(&self) -> String {
        format!("RowStream(chunk_size={})", self.chunk_size)
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
    /// The id of the last inserted row (SQLite's `rowid`; always 0 on Postgres, which uses `RETURNING`).
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

/// Stores a Python exception from `prepare` so `Database.sqlite` can raise it
/// as it is, and gives the engine an error to stop opening.
fn keep_error(kept: &Mutex<Option<PyErr>>, error: PyErr) -> djox::Error {
    let message = error.to_string();
    *kept.lock().unwrap() = Some(error);
    djox::Error::Settings(format!("prepare() failed: {message}"))
}
