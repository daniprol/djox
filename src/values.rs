//! Converts Python values to djox values, and result rows to Python.
//!
//! Python to database, the same way Django's SQLite backend adapts values:
//!
//! | Python                            | Stored as                             |
//! |-----------------------------------|---------------------------------------|
//! | `None`                            | `NULL`                                |
//! | `bool`                            | a boolean (SQLite: `1` or `0`)        |
//! | `int`, `float`, `str`             | integer, real, text                   |
//! | `bytes`, `bytearray`, `memoryview` | blob                                 |
//! | `datetime.datetime`               | text, `value.isoformat(" ")`          |
//! | `datetime.date`, `datetime.time`  | text, `value.isoformat()`             |
//! | `decimal.Decimal`, `uuid.UUID`    | text, `str(value)`                    |
//! | `datetime.timedelta`              | a duration (SQLite: microseconds)     |
//! | `ipaddress` addresses and networks | text, `str(value)`                   |
//! | psycopg's `Json` and `Jsonb`      | text, the JSON they hold              |
//! | psycopg's `Binary`                | blob, the bytes it holds              |
//! | `list`, `tuple`                   | text, a Postgres array: `{"a","b"}`   |
//!
//! Postgres reads the text it gets as its column's type (`2024-05-01` into a
//! `date` column), so these rules work for both backends. The last two rows
//! are values Django's Postgres backend prepares for psycopg.
//!
//! Database to Python: `NULL` -> `None`, integer -> `int`, real -> `float`,
//! text -> `str`, blob -> `bytes`. Rows are tuples, like Python's `sqlite3`
//! without `detect_types`. On SQLite, Django's field converters do the rest
//! (text to `datetime`, `1` to `True`...). Postgres returns typed values,
//! which become `bool`, `Decimal`, `date`, `time`, `datetime`, `timedelta`
//! and `uuid.UUID`, like psycopg returns them.

use djox::{Rows, Value};
use pyo3::exceptions::{PyOverflowError, PyTypeError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyBytes, PyFloat, PyInt, PyList, PyString, PyTuple};

/// `decimal.Decimal`, looked up once (a module-level cache).
static DECIMAL_CLASS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

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
        return Ok(Value::Boolean(boolean.is_true()));
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
    if value.is_instance(&datetime.getattr("timedelta")?)? {
        // `extract` reads the Python `timedelta` into Rust's `TimeDelta`.
        return Ok(Value::Duration(value.extract()?));
    }
    let builtins = py.import("builtins")?;
    if value.is_instance(&builtins.getattr("bytearray")?)? || value.is_instance(&builtins.getattr("memoryview")?)? {
        let bytes = builtins.getattr("bytes")?.call1((value,))?;
        return Ok(Value::Blob(bytes.cast::<PyBytes>()?.as_bytes().to_vec()));
    }
    let ipaddress = py.import("ipaddress")?;
    for class_name in [
        "IPv4Address",
        "IPv6Address",
        "IPv4Network",
        "IPv6Network",
        "IPv4Interface",
        "IPv6Interface",
    ] {
        if value.is_instance(&ipaddress.getattr(class_name)?)? {
            return Ok(Value::Text(value.str()?.to_str()?.to_owned()));
        }
    }
    if let Some(unwrapped) = psycopg_wrapper_value(value)? {
        return Ok(unwrapped);
    }
    if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        return Ok(Value::Text(array_text(value)?));
    }
    Err(PyTypeError::new_err(format!(
        "djox can't store a value of type {} in the database",
        value.get_type().name()?
    )))
}

/// The value inside one of psycopg's wrappers, which Django's Postgres
/// backend makes for psycopg: `Json` and `Jsonb` (a `JSONField`'s value) and
/// `Binary` (a `BinaryField`'s). `None` for anything else. (The class is
/// recognized by its module, so djox doesn't need psycopg installed.)
fn psycopg_wrapper_value(value: &Bound<'_, PyAny>) -> PyResult<Option<Value>> {
    let class = value.get_type();
    let module: String = class.getattr("__module__")?.extract()?;
    let name = class.name()?;
    if module == "psycopg.types.json" && (name == "Json" || name == "Jsonb") {
        return Ok(Some(json_text(value)?));
    }
    if module == "psycopg.dbapi20" && name == "Binary" {
        // `Binary(data)` keeps `data` (bytes, a bytearray or a memoryview) in `.obj`.
        let builtins = value.py().import("builtins")?;
        let bytes = builtins.getattr("bytes")?.call1((value.getattr("obj")?,))?;
        return Ok(Some(Value::Blob(bytes.cast::<PyBytes>()?.as_bytes().to_vec())));
    }
    Ok(None)
}

/// A list as the text of a Postgres array, like psycopg sends it:
/// `["a", 1, None]` is `{"a","1",NULL}`. Each item is quoted (Postgres reads
/// it as the array's item type), with `"` and `\` escaped; a list inside
/// a list is an array inside the array. Django sends lists for Postgres
/// arrays, like the path of a `JSONField` key (`data__a__1`).
fn array_text(items: &Bound<'_, PyAny>) -> PyResult<String> {
    let mut words = Vec::new();
    for item in items.try_iter()? {
        let item = item?;
        if item.is_none() {
            words.push("NULL".to_string());
        } else if item.is_instance_of::<PyList>() || item.is_instance_of::<PyTuple>() {
            words.push(array_text(&item)?);
        } else {
            let text = match value_from_python(&item)? {
                Value::Text(text) => text,
                Value::Boolean(boolean) => boolean.to_string(),
                Value::Integer(number) => number.to_string(),
                Value::Real(number) => format!("{number:?}"),
                _ => item.str()?.to_str()?.to_owned(),
            };
            let escaped = text.replace('\\', "\\\\").replace('"', "\\\"");
            words.push(format!("\"{escaped}\""));
        }
    }
    Ok(format!("{{{}}}", words.join(",")))
}

/// The JSON text inside psycopg's wrapper: `wrapper.dumps(wrapper.obj)`, or
/// `json.dumps(wrapper.obj)` when it has no `dumps` of its own.
fn json_text(wrapper: &Bound<'_, PyAny>) -> PyResult<Value> {
    let py = wrapper.py();
    let mut dumps = wrapper.getattr("dumps")?;
    if dumps.is_none() {
        dumps = py.import("json")?.getattr("dumps")?;
    }
    let text = dumps.call1((wrapper.getattr("obj")?,))?;
    Ok(Value::Text(text.extract()?))
}

/// One djox value as a Python object.
pub fn value_to_python<'py>(py: Python<'py>, value: Value) -> PyResult<Bound<'py, PyAny>> {
    let object = match value {
        Value::Null => py.None().into_bound(py),
        Value::Integer(number) => number.into_pyobject(py)?.into_any(),
        Value::Real(number) => PyFloat::new(py, number).into_any(),
        Value::Text(text) => PyString::new(py, &text).into_any(),
        Value::Blob(bytes) => PyBytes::new(py, &bytes).into_any(),
        // The typed values only come from Postgres (see the table at the top of this file).
        Value::Boolean(boolean) => PyBool::new(py, boolean).to_owned().into_any(),
        Value::Decimal(digits) => DECIMAL_CLASS.import(py, "decimal", "Decimal")?.call1((digits,))?,
        // PyO3 turns chrono's dates and times into Python's `datetime` objects
        // (its `chrono` feature), and a UTC moment into one with `tzinfo=timezone.utc`.
        Value::Date(date) => date.into_pyobject(py)?.into_any(),
        Value::Time(time) => time.into_pyobject(py)?.into_any(),
        Value::DateTime(moment) => moment.into_pyobject(py)?.into_any(),
        Value::DateTimeUtc(moment) => moment.into_pyobject(py)?.into_any(),
        Value::Duration(length) => length.into_pyobject(py)?.into_any(),
        Value::Uuid(uuid) => uuid.into_pyobject(py)?.into_any(),
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
