//! `djox.Database`: the Python class around a backend's [`djox::Database`].
//!
//! The class holds the backend behind the `Database` trait, so the same
//! Python class (and every method on it) works for every backend. Each
//! backend only adds a constructor: `Database.sqlite(...)` and
//! `Database.postgres(...)`.
//!
//! Each method sends its statement to the backend right away and returns an
//! `asyncio.Future` that the backend finishes when the answer arrives (see
//! `answers.rs`), so Python's event loop is never blocked. The one exception
//! is a *quick read* or *write* (on SQLite): a small statement the backend
//! runs right away, within a limit (1 ms by default), because sending it to
//! another thread would take longer than running it. Its Future is already
//! finished when it is returned. A Future rather
//! than a coroutine: the statement is already on its way before anyone awaits
//! it, so statements reach the database in the order the methods were called.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use djox::postgres::{PostgresDatabase, PostgresSettings, TlsMode};
use djox::sqlite::{SharedWriter, SqliteConnection, SqliteDatabase, SqliteSettings};
use djox::{Database, Rows, Value};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyTuple;

use crate::answers::{finished_future, future_for_answer, future_for_stream_answer};
use crate::connection::PythonConnection;
use crate::errors::{NotSupportedError, to_python_error};
use crate::values::{columns_and_rows_to_python, parameters_from_python, row_to_python, rows_to_python};

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
    /// SQLite's writer connection, for `writer_connection()`; `None` on Postgres.
    sqlite_writer: Option<Arc<SharedWriter>>,
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
    /// `quick_read_limit` is the most seconds a read may run right on the
    /// calling thread before it goes to a reader instead (see
    /// `crates/djox/src/sqlite/quick_reads.rs`); 0 turns quick reads off.
    /// `quick_write_limit` is the same for a write, which otherwise goes to
    /// the writer thread (see `crates/djox/src/sqlite/writer.rs`).
    ///
    /// `#[staticmethod]` is Python's `@staticmethod`. `#[pyo3(signature = ...)]`
    /// writes the Python signature, defaults and keyword-only `*` included.
    #[staticmethod]
    #[pyo3(signature = (
        path, *, readers = None, timeout = 5.0, quick_read_limit = 0.001, quick_write_limit = 0.001, prepare = None,
    ))]
    fn sqlite(
        py: Python<'_>,
        path: PathBuf,
        readers: Option<usize>,
        timeout: f64,
        quick_read_limit: f64,
        quick_write_limit: f64,
        prepare: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        let mut settings = SqliteSettings::new(path.clone());
        settings.busy_timeout = seconds("timeout", timeout)?;
        settings.quick_read_limit = seconds("quick_read_limit", quick_read_limit)?;
        settings.quick_write_limit = seconds("quick_write_limit", quick_write_limit)?;
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
            sqlite_writer: Some(backend.writer()),
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
    /// `sslmode`, `sslrootcert`, `sslcert` and `sslkey` are libpq's TLS
    /// settings, with libpq's values (`sslmode="verify-full"`).
    ///
    /// It opens one connection right away, so wrong settings fail here.
    #[staticmethod]
    #[pyo3(signature = (
        connection, *, pool_size = 10, time_zone = None,
        sslmode = None, sslrootcert = None, sslcert = None, sslkey = None,
    ))]
    // Clippy counts arguments; these are the keyword arguments of one Python method.
    #[allow(clippy::too_many_arguments)]
    fn postgres(
        py: Python<'_>,
        connection: String,
        pool_size: usize,
        time_zone: Option<String>,
        sslmode: Option<String>,
        sslrootcert: Option<String>,
        sslcert: Option<String>,
        sslkey: Option<String>,
    ) -> PyResult<Self> {
        let mut settings = PostgresSettings::new(connection);
        settings.pool_size = pool_size;
        settings.time_zone = time_zone;
        // `if let Some(name) = ...`: only when `sslmode` isn't `None`.
        if let Some(name) = sslmode {
            settings.tls.mode = Some(TlsMode::from_name(&name).map_err(to_python_error)?);
        }
        settings.tls.root_certificates = sslrootcert;
        settings.tls.client_certificate = sslcert;
        settings.tls.client_key = sslkey;
        let location = settings.describe();
        // Connecting waits for the network: `detach` lets other Python threads run meanwhile.
        let backend = py
            .detach(|| PostgresDatabase::open(settings))
            .map_err(to_python_error)?;
        Ok(PythonDatabase {
            backend: Arc::new(backend),
            sqlite_writer: None,
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
        if let Some(rows) = self.fetch_all_quickly(py, &sql, &parameters) {
            return finished_future(py, rows_to_python(py, rows)?.into_any());
        }
        // `|py, rows: Rows| ...` is a closure, Rust's `lambda`.
        let (future, reply) = future_for_answer(py, |py, rows: Rows| Ok(rows_to_python(py, rows)?.into_any()))?;
        self.backend.fetch_all(sql, parameters, reply);
        Ok(future)
    }

    /// Runs a query and returns `(columns, rows)`: the column names, then
    /// all the rows. Raw queries need the names, to know which column is which field.
    #[pyo3(signature = (sql, parameters = None))]
    fn fetch_all_with_columns<'py>(
        &self,
        py: Python<'py>,
        sql: String,
        parameters: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let parameters = parameters_from_python(parameters)?;
        if let Some(rows) = self.fetch_all_quickly(py, &sql, &parameters) {
            return finished_future(py, columns_and_rows_to_python(py, rows)?.into_any());
        }
        let (future, reply) =
            future_for_answer(
                py,
                |py, rows: Rows| Ok(columns_and_rows_to_python(py, rows)?.into_any()),
            )?;
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
        if let Some(rows) = self.fetch_all_quickly(py, &sql, &parameters) {
            return finished_future(py, first_row_to_python(py, rows)?);
        }
        let (future, reply) = future_for_answer(py, first_row_to_python)?;
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
        // A quick write (see the module docs), when the backend can.
        if let Some(executed) = py.detach(|| self.backend.execute_quickly(&sql, &parameters)) {
            return finished_future(py, executed_to_python(py, executed)?);
        }
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

    /// A `djox.Connection` on SQLite's writer connection itself, for
    /// `djox.atomic()`: its transaction runs on the connection the writer
    /// thread uses, so its small statements can run right on the calling
    /// thread (`Cursor.execute_quickly()`), and no other statement runs on
    /// the writer until it ends (see `crates/djox/src/sqlite/writer.rs`).
    ///
    /// Closing it rolls back its open transaction, and leaves the writer
    /// connection open: that belongs to the database. Postgres has no writer
    /// connection: `NotSupportedError`.
    fn writer_connection(&self) -> PyResult<PythonConnection> {
        match &self.sqlite_writer {
            Some(writer) => Ok(PythonConnection::share_writer(
                Arc::clone(writer),
                self.location.clone(),
            )),
            None => Err(NotSupportedError::new_err(
                "only a SQLite database has a writer connection",
            )),
        }
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

impl PythonDatabase {
    /// A quick read (see the module docs), or `None` to send the query as usual.
    fn fetch_all_quickly(&self, py: Python<'_>, sql: &str, parameters: &[Value]) -> Option<Rows> {
        // The event loop's thread waits for the query either way, but other
        // Python threads can run meanwhile: let go of the GIL. (A Python SQL
        // function the query calls takes it back for its own call.)
        py.detach(|| self.backend.fetch_all_quickly(sql, parameters))
    }
}

/// The first row of `rows` as a tuple, or `None` if there is none (`fetch_one`).
fn first_row_to_python(py: Python<'_>, rows: Rows) -> PyResult<Bound<'_, PyAny>> {
    match rows.rows.into_iter().next() {
        Some(row) => Ok(row_to_python(py, row)?.into_any()),
        None => Ok(py.None().into_bound(py)),
    }
}

/// `value` seconds as a `Duration`, or a `ValueError` naming the setting.
fn seconds(name: &str, value: f64) -> PyResult<Duration> {
    if !(value.is_finite() && value >= 0.0) {
        return Err(PyValueError::new_err(format!(
            "{name} must be a number of seconds, 0 or more"
        )));
    }
    Ok(Duration::from_secs_f64(value))
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
