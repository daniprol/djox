//! Python functions and aggregate classes as SQL functions.
//!
//! The `djox` crate can register any Rust closure as a SQL function (see
//! `crates/djox/src/sqlite/functions.rs`). This file wraps Python callables in
//! such closures, so `connection.create_function()` and
//! `connection.create_aggregate()` work like in Python's `sqlite3`. Django's
//! own `register()` uses exactly those two methods.
//!
//! SQLite calls a function in the middle of running a statement, on whatever
//! thread runs it, and that thread doesn't hold Python's lock (the GIL). So
//! each call takes the GIL first, with `Python::attach`.

use djox::Value;
use djox::sqlite::AggregateState;
use pyo3::prelude::*;

use crate::values::{row_to_python, value_from_python};

/// A Python callable as a SQL scalar function: SQL arguments in, one value out.
///
/// `impl Fn(...) + Send + 'static` in the return type means "some closure
/// with this signature, that can move to another thread and borrows nothing".
pub fn scalar_function(function: Py<PyAny>) -> impl Fn(Vec<Value>) -> djox::Result<Value> + Send + 'static {
    move |arguments: Vec<Value>| {
        Python::attach(|py| {
            let arguments = row_to_python(py, arguments).map_err(function_error)?;
            let result = function.call1(py, arguments).map_err(function_error)?;
            value_from_python(result.bind(py)).map_err(function_error)
        })
    }
}

/// A Python aggregate class as a SQL aggregate: SQLite makes one instance per
/// group of rows, calls `step(*arguments)` for each row and `finalize()` at the end.
pub fn new_aggregate_state(aggregate_class: Py<PyAny>) -> djox::sqlite::NewAggregateState {
    Box::new(move || {
        Python::attach(|py| {
            let instance = aggregate_class.call0(py).map_err(function_error)?;
            let state: Box<dyn AggregateState> = Box::new(PythonAggregateState { instance });
            Ok(state)
        })
    })
}

/// One instance of a Python aggregate class.
struct PythonAggregateState {
    instance: Py<PyAny>,
}

impl AggregateState for PythonAggregateState {
    fn step(&mut self, arguments: Vec<Value>) -> djox::Result<()> {
        Python::attach(|py| {
            let arguments = row_to_python(py, arguments).map_err(function_error)?;
            self.instance
                .call_method1(py, "step", arguments)
                .map_err(function_error)?;
            Ok(())
        })
    }

    fn finish(&mut self) -> djox::Result<Value> {
        Python::attach(|py| {
            let result = self.instance.call_method0(py, "finalize").map_err(function_error)?;
            value_from_python(result.bind(py)).map_err(function_error)
        })
    }
}

/// A Python exception raised inside a SQL function, as an engine error. The
/// statement fails with an `OperationalError`, like in Python's `sqlite3`.
fn function_error(error: PyErr) -> djox::Error {
    djox::Error::Operational(format!("user-defined function raised exception: {error}"))
}
