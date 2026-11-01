//! `djox.Connection`: one SQLite connection used from sync code, the way
//! Django's sync ORM uses Python's `sqlite3`.
//!
//! Django's SQLite backend expects a connection object shaped like
//! `sqlite3.Connection`: `cursor()`, `commit()`, `rollback()`, `close()`,
//! `create_function()`... This class has those methods, but its SQLite is the
//! copy bundled in djox. So Django's sync ORM and djox's async `Database`
//! share one SQLite in the process, and always see each other's locks.
//!
//! Each method runs on the caller's thread and blocks it, like `sqlite3`
//! does. While SQLite works, the GIL is released, so other Python threads
//! keep running.
//!
//! # The one locking rule
//!
//! A connection is guarded by a `Mutex` (like `threading.Lock`). The GIL is
//! a lock too. If thread A held the GIL and waited for the connection while
//! thread B held the connection and ran a Python SQL function (which needs
//! the GIL), both would wait forever. So: **release the GIL first, then take
//! the connection** (`py.detach(...)` around every `lock()`).

use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use djox::Value;
use djox::sqlite::{Access, SqliteConnection, SqliteSettings, StatementOutput, is_data_change};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::cursor::Cursor;
use crate::errors::to_python_error;
use crate::functions::{new_aggregate_state, scalar_function};

/// One SQLite connection for sync code, shaped like Python's `sqlite3.Connection`.
///
/// `frozen` means Python can't set attributes on it; what changes (the
/// connection, `autocommit`) sits behind a `Mutex`, so threads can share it.
#[pyclass(name = "Connection", module = "djox", frozen)]
pub struct PythonConnection {
    /// The connection, or `None` once it is closed (or given back to the
    /// async database that lent it, see [`PythonConnection::lend`]).
    sqlite: Mutex<Option<SqliteConnection>>,
    /// `False` makes a write open a transaction that stays open until
    /// `commit()` or `rollback()` (see the `autocommit` property).
    autocommit: Mutex<bool>,
    /// Where the database is, for `repr()`.
    location: String,
}

impl PythonConnection {
    /// Wraps a connection the async `Database` is opening, so Python code can
    /// prepare it. [`PythonConnection::take_back`] returns it afterwards.
    pub fn lend(sqlite: SqliteConnection, location: String) -> PythonConnection {
        PythonConnection {
            sqlite: Mutex::new(Some(sqlite)),
            autocommit: Mutex::new(true),
            location,
        }
    }

    /// Takes the connection out again; the Python object is closed from now on.
    pub fn take_back(&self) -> Option<SqliteConnection> {
        // `.take()` moves the value out of the `Option` and leaves `None`.
        self.sqlite.lock().unwrap().take()
    }

    /// Runs one statement (already in SQLite's `?` style) with the GIL released.
    pub fn run(&self, py: Python<'_>, sql: &str, parameters: Vec<Value>) -> PyResult<StatementOutput> {
        let autocommit = *self.autocommit.lock().unwrap();
        self.with_connection(py, |sqlite| {
            begin_before_a_write_if_needed(sqlite, autocommit, sql)?;
            sqlite.run(sql, &parameters)
        })
    }

    /// Runs one statement once per list of parameters, with the GIL released.
    pub fn run_many(&self, py: Python<'_>, sql: &str, parameter_lists: Vec<Vec<Value>>) -> PyResult<StatementOutput> {
        let autocommit = *self.autocommit.lock().unwrap();
        self.with_connection(py, |sqlite| {
            begin_before_a_write_if_needed(sqlite, autocommit, sql)?;
            sqlite.run_many(sql, &parameter_lists)
        })
    }

    /// Calls `action` with the open connection (and the GIL released), or
    /// fails with `DatabaseClosedError`.
    fn with_connection<T: Send>(
        &self,
        py: Python<'_>,
        action: impl FnOnce(&mut SqliteConnection) -> djox::Result<T> + Send,
    ) -> PyResult<T> {
        py.detach(|| {
            let mut guard = self.sqlite.lock().unwrap();
            // `guard.as_mut()` is the connection inside the `Option`, or `None` if closed.
            match guard.as_mut() {
                Some(sqlite) => action(sqlite),
                None => Err(djox::Error::Closed),
            }
        })
        .map_err(to_python_error)
    }
}

/// With `autocommit` off, Python's `sqlite3` opens a transaction right before
/// the first `INSERT`, `UPDATE`, `DELETE` or `REPLACE`. Django relies on it
/// for `transaction.set_autocommit(False)`.
fn begin_before_a_write_if_needed(sqlite: &mut SqliteConnection, autocommit: bool, sql: &str) -> djox::Result<()> {
    if !autocommit && !sqlite.in_transaction() && is_data_change(sql) {
        sqlite.run("BEGIN", &[])?;
    }
    Ok(())
}

#[pymethods]
impl PythonConnection {
    /// Opens a SQLite database file (created if missing) and puts it in WAL
    /// mode. `":memory:"` works too, for a private in-memory database.
    #[staticmethod]
    #[pyo3(signature = (path, *, timeout = 5.0))]
    fn sqlite(py: Python<'_>, path: PathBuf, timeout: f64) -> PyResult<Self> {
        if !(timeout.is_finite() && timeout >= 0.0) {
            return Err(PyValueError::new_err("timeout must be a number of seconds, 0 or more"));
        }
        let mut settings = SqliteSettings::new(path.clone());
        settings.busy_timeout = Duration::from_secs_f64(timeout);
        let sqlite = py
            .detach(|| SqliteConnection::open(&settings, Access::Writer))
            .map_err(to_python_error)?;
        Ok(PythonConnection::lend(sqlite, path.display().to_string()))
    }

    /// A new cursor on this connection.
    ///
    /// `this: Py<Self>` is a reference to the Python object itself (like
    /// `self` in Python), which the cursor keeps.
    fn cursor(this: Py<Self>) -> Cursor {
        Cursor::new(this)
    }

    /// A shortcut for `connection.cursor().execute(sql, parameters)`.
    #[pyo3(signature = (sql, parameters = None))]
    fn execute<'py>(
        this: Py<Self>,
        py: Python<'py>,
        sql: &str,
        parameters: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, Cursor>> {
        let cursor = Bound::new(py, Cursor::new(this))?;
        cursor.borrow_mut().run(py, sql, parameters)?;
        Ok(cursor)
    }

    /// A shortcut for `connection.cursor().executemany(sql, parameter_lists)`.
    fn executemany<'py>(
        this: Py<Self>,
        py: Python<'py>,
        sql: &str,
        parameter_lists: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, Cursor>> {
        let cursor = Bound::new(py, Cursor::new(this))?;
        cursor.borrow_mut().run_many(py, sql, parameter_lists)?;
        Ok(cursor)
    }

    /// Commits the open transaction, if there is one.
    fn commit(&self, py: Python<'_>) -> PyResult<()> {
        self.with_connection(py, |sqlite| {
            if sqlite.in_transaction() {
                sqlite.run("COMMIT", &[])?;
            }
            Ok(())
        })
    }

    /// Rolls back the open transaction, if there is one.
    fn rollback(&self, py: Python<'_>) -> PyResult<()> {
        self.with_connection(py, |sqlite| {
            if sqlite.in_transaction() {
                sqlite.run("ROLLBACK", &[])?;
            }
            Ok(())
        })
    }

    /// Closes the connection; an open transaction is rolled back. Closing
    /// twice does nothing.
    fn close(&self, py: Python<'_>) {
        // Dropping the connection closes it. That can wait on the disk, so
        // other Python threads may run meanwhile.
        py.detach(|| drop(self.take_back()));
    }

    /// Whether `close()` was called.
    #[getter]
    fn closed(&self, py: Python<'_>) -> bool {
        // The one locking rule: let go of the GIL before taking the connection.
        py.detach(|| self.sqlite.lock().unwrap().is_none())
    }

    /// Whether a transaction is open.
    #[getter]
    fn in_transaction(&self, py: Python<'_>) -> PyResult<bool> {
        self.with_connection(py, |sqlite| Ok(sqlite.in_transaction()))
    }

    /// `True` (the default): every statement commits on its own, unless
    /// you run `BEGIN` yourself. `False`: a write opens a transaction that
    /// stays open until `commit()` or `rollback()`, like `sqlite3` with
    /// `isolation_level=""`. Django sets it for `transaction.set_autocommit()`.
    #[getter]
    fn autocommit(&self) -> bool {
        *self.autocommit.lock().unwrap()
    }

    #[setter]
    fn set_autocommit(&self, autocommit: bool) {
        *self.autocommit.lock().unwrap() = autocommit;
    }

    /// Registers a Python function that SQL can call, like
    /// `sqlite3.Connection.create_function()`. `argument_count` -1 means any number.
    #[pyo3(signature = (name, argument_count, function, *, deterministic = false))]
    fn create_function(
        &self,
        py: Python<'_>,
        name: String,
        argument_count: i32,
        function: Py<PyAny>,
        deterministic: bool,
    ) -> PyResult<()> {
        let function = scalar_function(function);
        self.with_connection(py, move |sqlite| {
            sqlite.create_scalar_function(&name, argument_count, deterministic, function)
        })
    }

    /// Registers a Python aggregate class (with `step()` and `finalize()`),
    /// like `sqlite3.Connection.create_aggregate()`.
    fn create_aggregate(
        &self,
        py: Python<'_>,
        name: String,
        argument_count: i32,
        aggregate_class: Py<PyAny>,
    ) -> PyResult<()> {
        let new_state = new_aggregate_state(aggregate_class);
        self.with_connection(py, move |sqlite| {
            sqlite.create_aggregate_function(&name, argument_count, new_state)
        })
    }

    /// One of SQLite's limits, like `sqlite3.Connection.getlimit()`
    /// (`sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER` and friends).
    fn getlimit(&self, py: Python<'_>, category: i32) -> PyResult<i32> {
        self.with_connection(py, |sqlite| sqlite.limit(category))
    }

    /// Changes one of SQLite's limits for this connection and returns its old
    /// value, like `sqlite3.Connection.setlimit()`.
    fn setlimit(&self, py: Python<'_>, category: i32, limit: i32) -> PyResult<i32> {
        self.with_connection(py, |sqlite| sqlite.set_limit(category, limit))
    }

    fn __repr__(&self, py: Python<'_>) -> String {
        let closed = if self.closed(py) { ", closed" } else { "" };
        format!("Connection.sqlite('{}'{closed})", self.location)
    }
}
