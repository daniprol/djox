//! Converts Python values to djox values, and result rows to Python.
//!
//! Python to database, the same way Django's SQLite backend adapts values:
//!
//! | Python                            | Stored as                             |
//! |-----------------------------------|---------------------------------------|
//! | `None`                            | `NULL`                                |
//! | `bool`                            | `1` or `0`                            |
//! | `int`, `float`, `str`             | integer, real, text                   |
//! | `bytes`, `bytearray`, `memoryview` | blob                                 |
//! | `datetime.datetime`               | text, `value.isoformat(" ")`          |
//! | `datetime.date`, `datetime.time`  | text, `value.isoformat()`             |
//! | `decimal.Decimal`, `uuid.UUID`    | text, `str(value)`                    |
//!
//! Database to Python: `NULL` -> `None`, integer -> `int`, real -> `float`,
//! text -> `str`, blob -> `bytes`. Rows are tuples, like Python's `sqlite3`
//! without `detect_types`. Django's field converters do the rest (text to
//! `datetime`, `1` to `True`...).

use djox::{Rows, Value};
use pyo3::exceptions::{PyOverflowError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyFloat, PyInt, PyList, PyString, PyTuple};

/// One query's parameters (`None`, or a list or tuple of values) as djox values.
pub fn parameters_from_python(parameters: Option<&Bound<'_, PyAny>>) -> PyResult<Vec<Value>> {
    let Some(parameters) = parameters else {
        return Ok(Vec::new());
    };
    if parameters.is_none() {
        return Ok(Vec::new());
    }
    if parameters.is_instance_of::<PyString>() || parameters.is_instance_of::<PyBytes>() {
        return Err(PyTypeError::new_err(
            "parameters must be a list or tuple of values, not a single string",
        ));
    }
    let mut values = Vec::new();
    for parameter in parameters.try_iter()? {
        values.push(value_from_python(&parameter?)?);
    }
    Ok(values)
}

/// One Python value as a djox value (see the table at the top of this file).
pub fn value_from_python(value: &Bound<'_, PyAny>) -> PyResult<Value> {
    // The common types first, with fast exact checks. `bool` comes before
    // `int` because in Python `True` is also an `int`.
    if value.is_none() {
        return Ok(Value::Null);
    }
    if let Ok(boolean) = value.cast::<PyBool>() {
        return Ok(Value::Integer(i64::from(boolean.is_true())));
    }
    if value.is_instance_of::<PyInt>() {
        let number: i64 = value
            .extract()
            .map_err(|_| PyOverflowError::new_err("Python int too large to store in SQLite (the limit is 64 bits)"))?;
        return Ok(Value::Integer(number));
    }
    if let Ok(text) = value.cast::<PyString>() {
        return Ok(Value::Text(text.to_str()?.to_owned()));
    }
    if value.is_instance_of::<PyFloat>() {
        return Ok(Value::Real(value.extract()?));
    }
    if let Ok(bytes) = value.cast::<PyBytes>() {
        return Ok(Value::Blob(bytes.as_bytes().to_vec()));
    }
    rarer_value_from_python(value)
}

/// Dates, times, decimals, UUIDs and byte buffers. They are rare in
/// parameters (Django already turns most of them into text), so a slower
/// check by class is fine here.
fn rarer_value_from_python(value: &Bound<'_, PyAny>) -> PyResult<Value> {
    let py = value.py();
    let datetime = py.import("datetime")?;
    // `datetime.datetime` is a subclass of `datetime.date`, so it is checked first.
    if value.is_instance(&datetime.getattr("datetime")?)? {
        return Ok(Value::Text(value.call_method1("isoformat", (" ",))?.extract()?));
    }
    if value.is_instance(&datetime.getattr("date")?)? || value.is_instance(&datetime.getattr("time")?)? {
        return Ok(Value::Text(value.call_method0("isoformat")?.extract()?));
    }
    if value.is_instance(&py.import("decimal")?.getattr("Decimal")?)?
        || value.is_instance(&py.import("uuid")?.getattr("UUID")?)?
    {
        return Ok(Value::Text(value.str()?.to_str()?.to_owned()));
    }
    let builtins = py.import("builtins")?;
    if value.is_instance(&builtins.getattr("bytearray")?)? || value.is_instance(&builtins.getattr("memoryview")?)? {
        let bytes = builtins.getattr("bytes")?.call1((value,))?;
        return Ok(Value::Blob(bytes.cast::<PyBytes>()?.as_bytes().to_vec()));
    }
    Err(PyTypeError::new_err(format!(
        "djox can't store a value of type {} in the database",
        value.get_type().name()?
    )))
}

/// One djox value as a Python object.
fn value_to_python<'py>(py: Python<'py>, value: Value) -> PyResult<Bound<'py, PyAny>> {
    let object = match value {
        Value::Null => py.None().into_bound(py),
        Value::Integer(number) => number.into_pyobject(py)?.into_any(),
        Value::Real(number) => PyFloat::new(py, number).into_any(),
        Value::Text(text) => PyString::new(py, &text).into_any(),
        Value::Blob(bytes) => PyBytes::new(py, &bytes).into_any(),
    };
    Ok(object)
}

/// One row as a Python tuple.
pub fn row_to_python<'py>(py: Python<'py>, row: Vec<Value>) -> PyResult<Bound<'py, PyTuple>> {
    let mut items = Vec::with_capacity(row.len());
    for value in row {
        items.push(value_to_python(py, value)?);
    }
    PyTuple::new(py, items)
}

/// All rows as a Python list of tuples, like `cursor.fetchall()`.
pub fn rows_to_python(py: Python<'_>, rows: Rows) -> PyResult<Bound<'_, PyList>> {
    let list = PyList::empty(py);
    for row in rows.rows {
        list.append(row_to_python(py, row)?)?;
    }
    Ok(list)
}
