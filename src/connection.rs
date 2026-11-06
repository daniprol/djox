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
//!
//! # A connection on the writer
//!
//! `Database.writer_connection()` makes a `djox.Connection` on the async
//! database's own writer connection, shared with its writer thread (see
//! `crates/djox/src/sqlite/writer.rs`). `djox.atomic()` runs its transaction
//! there, and its cursors can run a small statement right away
//! (`Cursor.execute_quickly()`), without waiting for anything.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use djox::Value;
use djox::sqlite::{
    Access, QuickOutcome, SharedWriter, SqliteConnection, SqliteSettings, StatementOutput, is_data_change,
};
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
    /// Where the SQLite connection is.
    sqlite: Place,
    /// `False` makes a write open a transaction that stays open until
    /// `commit()` or `rollback()` (see the `autocommit` property).
    autocommit: Mutex<bool>,
    /// Where the database is, for `repr()`.
    location: String,
}

/// Where a `djox.Connection`'s SQLite connection is. (A Rust `enum` is a
/// value that is exactly one of a few kinds, each with its own data.)
enum Place {
    /// A connection of its own, or `None` once it is closed (or given back to
    /// the async database that lent it, see [`PythonConnection::lend`]).
    Own(Mutex<Option<SqliteConnection>>),
    /// The writer connection of an async database, shared with its writer
    /// thread, or `None` once this `djox.Connection` is closed; and this
    /// connection's number there, which tells whose transaction is open.
    Writer(Mutex<Option<Arc<SharedWriter>>>, u64),
}

impl PythonConnection {
    /// Wraps a connection the async `Database` is opening, so Python code can
    /// prepare it. [`PythonConnection::take_back`] returns it afterwards.
    pub fn lend(sqlite: SqliteConnection, location: String) -> PythonConnection {
        PythonConnection {
            sqlite: Place::Own(Mutex::new(Some(sqlite))),
            autocommit: Mutex::new(true),
            location,
        }
    }

    /// A connection on an async database's writer connection (see the module docs).
    pub fn share_writer(writer: Arc<SharedWriter>, location: String) -> PythonConnection {
        let user = writer.new_user_number();
        PythonConnection {
            sqlite: Place::Writer(Mutex::new(Some(writer)), user),
            autocommit: Mutex::new(true),
            location,
        }
    }

    /// Takes the connection out again; the Python object is closed from now on.
    pub fn take_back(&self) -> Option<SqliteConnection> {
        match &self.sqlite {
            // `.take()` moves the value out of the `Option` and leaves `None`.
            Place::Own(sqlite) => sqlite.lock().unwrap().take(),
            Place::Writer(..) => None,
        }
    }

    /// Runs one statement (already in SQLite's `?` style) with the GIL released.
    pub fn run(&self, py: Python<'_>, sql: &str, parameters: Vec<Value>) -> PyResult<StatementOutput> {
        let autocommit = *self.autocommit.lock().unwrap();
        let started = Instant::now();
        let output = self.with_connection(py, |sqlite| {
            begin_before_a_write_if_needed(sqlite, autocommit, sql)?;
            sqlite.run(sql, &parameters)
        });
        if let Some((writer, _)) = self.writer() {
            // So that the next run of it knows whether it is quick (`execute_quickly()`).
            writer.remember_how_long(sql, started.elapsed());
        }
        output
    }

    /// Runs one statement right here if that needs no waiting (see the module
    /// docs): its output, or `None` if it didn't run (a connection of its own
    /// never runs one quickly).
    pub fn run_quickly(&self, py: Python<'_>, sql: &str, parameters: Vec<Value>) -> PyResult<Option<StatementOutput>> {
        let Some((writer, user)) = self.writer() else {
            return Ok(None);
        };
        if !*self.autocommit.lock().unwrap() {
            return Ok(None); // a write may need a `BEGIN` first: `run()` knows
        }
        // A Python SQL function the statement calls takes the GIL back for its own call.
        match py.detach(|| writer.run_quickly(user, sql, &parameters)) {
            QuickOutcome::Ran(Ok(output)) => Ok(Some(output)),
            QuickOutcome::Ran(Err(error)) => Err(to_python_error(error)),
            QuickOutcome::GaveUp => Ok(None),
        }
    }

    /// The writer connection this connection shares, and its number there,
    /// if it is one on a writer and still open.
    fn writer(&self) -> Option<(Arc<SharedWriter>, u64)> {
        match &self.sqlite {
            Place::Own(_) => None,
            // `.clone()` on an `Arc` hands out one more reference to the same writer.
            Place::Writer(writer, user) => writer.lock().unwrap().clone().map(|writer| (writer, *user)),
        }
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
        py.detach(|| match &self.sqlite {
            Place::Own(sqlite) => {
                let mut guard = sqlite.lock().unwrap();
                // `guard.as_mut()` is the connection inside the `Option`, or `None` if closed.
                match guard.as_mut() {
                    Some(sqlite) => action(sqlite),
                    None => Err(djox::Error::Closed),
                }
            }
            Place::Writer(writer, user) => {
                let writer = writer.lock().unwrap().clone();
                match writer {
                    Some(writer) => writer.with_connection(*user, action),
                    None => Err(djox::Error::Closed),
                }
            }
        })
        .map_err(to_python_error)
    }
}

/// Dropping a connection on a writer (like `__del__`) rolls back the transaction
/// it left open: no other statement could run on the writer until it ended.
impl Drop for PythonConnection {
    fn drop(&mut self) {
        if let Place::Writer(writer, user) = &self.sqlite
            && let Some(writer) = writer.lock().unwrap().take()
        {
            writer.roll_back_without_waiting(*user);
        }
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
    /// twice does nothing. On a writer connection, only this `djox.Connection`
    /// closes: the writer connection belongs to its database.
    fn close(&self, py: Python<'_>) {
        match &self.sqlite {
            // Dropping the connection closes it. That can wait on the disk, so
            // other Python threads may run meanwhile.
            Place::Own(_) => py.detach(|| drop(self.take_back())),
            Place::Writer(writer, user) => {
                let writer = py.detach(|| writer.lock().unwrap().take());
                if let Some(writer) = writer {
                    // Never waits: `close()` may run on the event loop's thread.
                    py.detach(|| writer.roll_back_without_waiting(*user));
                }
            }
        }
    }

    /// Whether `close()` was called.
    #[getter]
    fn closed(&self, py: Python<'_>) -> bool {
        // The one locking rule: let go of the GIL before taking the connection.
        py.detach(|| match &self.sqlite {
            Place::Own(sqlite) => sqlite.lock().unwrap().is_none(),
            Place::Writer(writer, _) => writer.lock().unwrap().is_none(),
        })
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
