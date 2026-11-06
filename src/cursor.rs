//! `djox.Cursor`: runs statements on a `djox.Connection` and hands back their
//! rows, like a cursor of Python's `sqlite3` (the DB-API, PEP 249).
//!
//! Django's ORM talks to every database through cursors: `execute()` a
//! statement, then `fetchone()`, `fetchmany()` or `fetchall()` its rows, and
//! read `rowcount` and `lastrowid`.
//!
//! Placeholders follow Django's SQLite backend:
//!
//! | `parameters`                | SQL is written with  | Example                                  |
//! |-----------------------------|----------------------|------------------------------------------|
//! | `None`                      | nothing: run as is   | `execute("SELECT '100%'")`               |
//! | a list or tuple             | `%s` (and `%%`)      | `execute("... WHERE id = %s", [1])`      |
//! | a dict (or other mapping)   | `%(name)s` (and `%%`) | `execute("... WHERE id = %(id)s", {"id": 1})` |
//!
//! A statement's rows are all read when it runs, then handed out by the
//! `fetch` methods. It is simple, and it frees the connection right away.

use std::collections::VecDeque;

use djox::Value;
use djox::sqlite::{StatementOutput, named_to_question_marks, to_question_marks};
use pyo3::exceptions::PyStopIteration;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyMapping, PyTuple};

use crate::connection::PythonConnection;
use crate::errors::ProgrammingError;
use crate::values::{parameters_from_python, row_to_python, value_from_python};

/// A cursor on a `djox.Connection`.
///
/// Without `frozen`, methods can take `&mut self` and change the cursor;
/// PyO3 checks at run time that only one call changes it at a time.
#[pyclass(name = "Cursor", module = "djox")]
pub struct Cursor {
    /// The connection the cursor runs statements on.
    connection: Py<PythonConnection>,
    /// The rows of the last statement that are not fetched yet. A `VecDeque`
    /// is a list that is fast to take from the front, like `collections.deque`.
    rows: VecDeque<Py<PyTuple>>,
    /// One `(name, None, None, None, None, None, None)` per column, or `None`
    /// if the last statement returns no rows (that is the DB-API's format).
    description: Option<Py<PyTuple>>,
    /// How many rows the last `INSERT`, `UPDATE`, `DELETE` or `REPLACE` changed, or -1.
    rowcount: i64,
    /// The id of the last inserted row, or `None` before any statement ran.
    lastrowid: Option<i64>,
    /// How many rows `fetchmany()` returns by default.
    arraysize: usize,
    closed: bool,
}

impl Cursor {
    pub fn new(connection: Py<PythonConnection>) -> Cursor {
        Cursor {
            connection,
            rows: VecDeque::new(),
            description: None,
            rowcount: -1,
            lastrowid: None,
            arraysize: 1,
            closed: false,
        }
    }

    /// Runs one statement and keeps what it gave back.
    pub fn run(&mut self, py: Python<'_>, sql: &str, parameters: Option<&Bound<'_, PyAny>>) -> PyResult<()> {
        self.check_open()?;
        let (sql, values) = sql_and_values(sql, parameters)?;
        let output = self.connection.get().run(py, &sql, values)?;
        self.keep(py, output)
    }

    /// Runs one statement once per item of `parameter_lists` and keeps what it gave back.
    pub fn run_many(&mut self, py: Python<'_>, sql: &str, parameter_lists: &Bound<'_, PyAny>) -> PyResult<()> {
        self.check_open()?;
        // The first item decides the placeholder style, like in Django's
        // backend. It may be a generator, so we read the items only once.
        let mut converted_sql: Option<String> = None;
        let mut lists: Vec<Vec<Value>> = Vec::new();
        for parameters in parameter_lists.try_iter()? {
            let (sql, values) = sql_and_values(sql, Some(&parameters?))?;
            // `get_or_insert` keeps the first converted SQL; later ones are the same.
            converted_sql.get_or_insert(sql);
            lists.push(values);
        }
        let sql = match converted_sql {
            Some(sql) => sql,
            // No parameter lists at all: nothing runs, but the SQL is still checked.
            None => to_question_marks(sql).into_owned(),
        };
        let output = self.connection.get().run_many(py, &sql, lists)?;
        self.keep(py, output)
    }

    /// Stores a statement's output as Python objects for the `fetch` methods.
    fn keep(&mut self, py: Python<'_>, output: StatementOutput) -> PyResult<()> {
        self.description = description_of(py, &output.rows.columns)?;
        self.rows.clear();
        for row in output.rows.rows {
            self.rows.push_back(row_to_python(py, row)?.unbind());
        }
        // `map_or(-1, ...)`: -1 if there is no count, else the count as an `i64`.
        self.rowcount = output.rows_affected.map_or(-1, |count| count as i64);
        self.lastrowid = Some(output.last_insert_id);
        Ok(())
    }

    fn check_open(&self) -> PyResult<()> {
        if self.closed {
            return Err(ProgrammingError::new_err("cannot use a closed cursor"));
        }
        Ok(())
    }
}

/// Django's SQL and parameters in SQLite's style: `?` placeholders and a
/// plain list of values (see the table at the top of this file).
fn sql_and_values(sql: &str, parameters: Option<&Bound<'_, PyAny>>) -> PyResult<(String, Vec<Value>)> {
    let Some(parameters) = parameters.filter(|parameters| !parameters.is_none()) else {
        return Ok((sql.to_string(), Vec::new()));
    };
    if let Ok(mapping) = parameters.cast::<PyMapping>() {
        let (sql, names) = named_to_question_marks(sql);
        let mut values = Vec::with_capacity(names.len());
        for name in names {
            let value = mapping
                .get_item(&name)
                .map_err(|_| ProgrammingError::new_err(format!("no value given for the parameter %({name})s")))?;
            values.push(value_from_python(&value)?);
        }
        return Ok((sql, values));
    }
    Ok((
        to_question_marks(sql).into_owned(),
        parameters_from_python(Some(parameters))?,
    ))
}

/// The DB-API `description`: a 7-tuple per column, of which SQLite only fills the name.
fn description_of(py: Python<'_>, columns: &[String]) -> PyResult<Option<Py<PyTuple>>> {
    if columns.is_empty() {
        return Ok(None);
    }
    let mut entries = Vec::with_capacity(columns.len());
    for name in columns {
        // `None::<i64>` is Python's `None` (Rust needs to know what it stands in for).
        let entry = (
            name.as_str(),
            None::<i64>,
            None::<i64>,
            None::<i64>,
            None::<i64>,
            None::<i64>,
            None::<i64>,
        );
        entries.push(entry.into_pyobject(py)?);
    }
    Ok(Some(PyTuple::new(py, entries)?.unbind()))
}

#[pymethods]
impl Cursor {
    /// Runs a statement. Returns the cursor, so `cursor.execute(...).fetchone()` works.
    #[pyo3(signature = (sql, parameters = None))]
    fn execute<'py>(
        mut this: PyRefMut<'py, Self>,
        sql: &str,
        parameters: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<PyRefMut<'py, Self>> {
        let py = this.py();
        this.run(py, sql, parameters)?;
        Ok(this)
    }

    /// Runs a statement right away if that needs no waiting, and returns
    /// `True`; then fetch its rows as after `execute()`. Returns `False` if it
    /// didn't run: run it with `execute()`, on a thread that may wait.
    ///
    /// Only a connection on a database's writer (`Database.writer_connection()`)
    /// runs statements quickly; for any other one this returns `False`.
    /// `djox.atomic()` uses it from the event loop, which must never wait.
    #[pyo3(signature = (sql, parameters = None))]
    fn execute_quickly(&mut self, py: Python<'_>, sql: &str, parameters: Option<&Bound<'_, PyAny>>) -> PyResult<bool> {
        self.check_open()?;
        let (sql, values) = sql_and_values(sql, parameters)?;
        match self.connection.get().run_quickly(py, &sql, values)? {
            Some(output) => {
                self.keep(py, output)?;
                Ok(true)
            }
            None => Ok(false),
        }
    }

    /// Runs a statement once per item of `parameter_lists` (an `INSERT`,
    /// `UPDATE`, `DELETE` or `REPLACE`). `rowcount` is the total.
    fn executemany<'py>(
        mut this: PyRefMut<'py, Self>,
        sql: &str,
        parameter_lists: &Bound<'py, PyAny>,
    ) -> PyResult<PyRefMut<'py, Self>> {
        let py = this.py();
        this.run_many(py, sql, parameter_lists)?;
        Ok(this)
    }

    /// The next row, or `None` when there are no more.
    fn fetchone(&mut self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.check_open()?;
        // `pop_front` takes the first row out, or gives `None` when empty.
        Ok(match self.rows.pop_front() {
            Some(row) => row.into_any(),
            None => py.None(),
        })
    }

    /// The next `size` rows (default: `arraysize`), fewer at the end.
    #[pyo3(signature = (size = None))]
    fn fetchmany<'py>(&mut self, py: Python<'py>, size: Option<usize>) -> PyResult<Bound<'py, PyList>> {
        self.check_open()?;
        let size = size.unwrap_or(self.arraysize);
        let list = PyList::empty(py);
        for _ in 0..size {
            let Some(row) = self.rows.pop_front() else { break };
            list.append(row)?;
        }
        Ok(list)
    }

    /// All the remaining rows.
    fn fetchall<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        self.check_open()?;
        // `drain(..)` takes every row out, leaving the queue empty.
        PyList::new(py, self.rows.drain(..))
    }

    /// Closes the cursor and drops the rows not fetched yet.
    fn close(&mut self) {
        self.closed = true;
        self.rows.clear();
    }

    #[getter]
    fn description(&self, py: Python<'_>) -> Option<Py<PyTuple>> {
        // `.as_ref().map(...)`: copy the reference to the tuple, if there is one.
        self.description.as_ref().map(|description| description.clone_ref(py))
    }

    #[getter]
    fn rowcount(&self) -> i64 {
        self.rowcount
    }

    #[getter]
    fn lastrowid(&self) -> Option<i64> {
        self.lastrowid
    }

    #[getter]
    fn arraysize(&self) -> usize {
        self.arraysize
    }

    #[setter]
    fn set_arraysize(&mut self, arraysize: usize) {
        self.arraysize = arraysize;
    }

    /// The connection this cursor runs statements on.
    #[getter]
    fn connection(&self, py: Python<'_>) -> Py<PythonConnection> {
        self.connection.clone_ref(py)
    }

    /// `for row in cursor:` fetches one row at a time.
    fn __iter__(this: PyRef<'_, Self>) -> PyRef<'_, Self> {
        this
    }

    fn __next__(&mut self) -> PyResult<Py<PyTuple>> {
        self.check_open()?;
        match self.rows.pop_front() {
            Some(row) => Ok(row),
            None => Err(PyStopIteration::new_err(())),
        }
    }

    /// DB-API methods that SQLite doesn't need; they do nothing.
    #[pyo3(signature = (*_sizes))]
    fn setinputsizes(&self, _sizes: &Bound<'_, PyTuple>) {}

    #[pyo3(signature = (*_arguments))]
    fn setoutputsize(&self, _arguments: &Bound<'_, PyTuple>) {}
}
