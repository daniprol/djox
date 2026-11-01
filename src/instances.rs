//! `djox.InstanceBuilder`: turns result rows into Django model instances, for the fast lane.
//!
//! Django builds one instance per row with `Model.from_db()`, which calls the
//! model's `__init__`. That runs a lot of general-purpose Python for every
//! row: sending the `pre_init` and `post_init` signals, matching positional
//! and keyword arguments, checking for deferred fields.
//!
//! For a plain model none of that changes the result, so the fast lane builds
//! the instance directly, the way `__init__` and `from_db()` leave it:
//!
//! ```python
//! book = object.__new__(Book)            # an empty instance, no __init__
//! book._state = ModelState()             # what __init__ starts with
//! book._state.adding = False             # what from_db() sets: it came from
//! book._state.db = "default"             # the database, through this alias
//! book.id = converted(row[0])            # one attribute per column, after
//! book.title = converted(row[1])         # Django's own converters
//! ```
//!
//! Python decides when this is allowed (`djox._fast_lane`): no signal
//! receivers, no custom `__init__` or `from_db()`. The converters (text to
//! `datetime`, a float to `Decimal`...) are the ones Django's compiler
//! chooses. The most common ones run in Rust (`conversions.rs`), the same
//! way Django's do; any other is Django's own Python function.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString, PyTuple};

use crate::conversions::{Conversion, PythonClasses};

/// Builds instances of one model from rows with the same columns.
///
/// Made once per query shape (`djox._fast_lane.statements`) and used for
/// every query of that shape. `frozen`: it never changes after it is made, so
/// any thread can use it.
#[pyclass(module = "djox", frozen)]
pub struct InstanceBuilder {
    /// The model class, like `Book`.
    model: Py<PyAny>,
    /// `object.__new__`, which makes an instance without calling `__init__`.
    new_object: Py<PyAny>,
    /// Django's `ModelState` class: every instance has one, as `_state`.
    model_state: Py<PyAny>,
    /// One column after another: the attribute it goes to, and how to convert it.
    columns: Vec<Column>,
    /// Django's `DatabaseWrapper`, which every converter receives.
    connection: Py<PyAny>,
    /// `datetime`, `Decimal`...: what the Rust conversions make.
    classes: PythonClasses,
}

/// What Python passes for one column's converters: `None`, or
/// `(functions, expression)`. (`type` gives a long type a short name, like a
/// type alias in Python.)
type ConvertersFromPython = Option<(Vec<Py<PyAny>>, Py<PyAny>)>;

/// One column of the rows.
struct Column {
    /// The attribute the value goes to, like `"title"` or `"author_id"`
    /// (Django's `attname`). Stored as a Python string, so it isn't made
    /// again for every row.
    attname: Py<PyString>,
    /// Django's converters for this column, applied in order, like
    /// `value = converter(value, expression, connection)`. Often none:
    /// integers and text need no converting.
    converters: Vec<Py<PyAny>>,
    /// The expression Django passes to the converters (the column, a `Col`).
    expression: Py<PyAny>,
    /// The same conversion as `converters`, done in Rust, when Python found
    /// one (`Option`: a `Conversion`, or `None`). For a value it leaves to
    /// Django, `converters` run instead.
    conversion: Option<Conversion>,
}

#[pymethods]
impl InstanceBuilder {
    /// `attnames` lists the attribute of each column. `converters` has one
    /// entry per column: `None`, or `(functions, expression)`, the way
    /// Django's `SQLCompiler.get_converters()` describes them. `conversions`
    /// has one entry per column too: `None`, or the Rust conversion that does
    /// the same as the column's converters (see `conversions.rs`).
    ///
    /// `#[new]` makes this the Python constructor, `InstanceBuilder(...)`.
    #[new]
    fn new(
        py: Python<'_>,
        model: Py<PyAny>,
        attnames: Vec<String>,
        converters: Vec<ConvertersFromPython>,
        conversions: Vec<Option<Bound<'_, PyTuple>>>,
        connection: Py<PyAny>,
        model_state: Py<PyAny>,
    ) -> PyResult<Self> {
        if attnames.len() != converters.len() || attnames.len() != conversions.len() {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "attnames, converters and conversions must have one entry per column",
            ));
        }
        let new_object = py.import("builtins")?.getattr("object")?.getattr("__new__")?.unbind();
        let mut columns = Vec::with_capacity(attnames.len());
        // `zip` walks two lists side by side, like Python's `zip()`.
        for ((attname, column_converters), description) in attnames.iter().zip(converters).zip(conversions) {
            // `match` on an `Option`: `Some(...)` when there are converters, `None` when not.
            let (functions, expression) = match column_converters {
                Some((functions, expression)) => (functions, expression),
                None => (Vec::new(), py.None()),
            };
            let conversion = match description {
                Some(description) => Some(Conversion::from_python(&description)?),
                None => None,
            };
            columns.push(Column {
                attname: PyString::intern(py, attname).unbind(),
                converters: functions,
                expression,
                conversion,
            });
        }
        Ok(InstanceBuilder {
            model,
            new_object,
            model_state,
            columns,
            connection,
            classes: PythonClasses::import(py)?,
        })
    }

    /// One instance per row, in order. `rows` is a list of tuples, as
    /// `fetch_all()` returns them; `alias` is the database alias the rows came
    /// from. `fetch_mode` (Django 6.1 and later) is set on each instance's state.
    #[pyo3(signature = (rows, alias, fetch_mode = None))]
    fn build<'py>(
        &self,
        py: Python<'py>,
        rows: &Bound<'py, PyList>,
        alias: &Bound<'py, PyString>,
        fetch_mode: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyList>> {
        let instances = PyList::empty(py);
        for row in rows.iter() {
            // `cast` checks the type, like `isinstance(row, tuple)`, and fails with a `TypeError`.
            let row = row.cast_into::<PyTuple>()?;
            instances.append(self.build_one(py, &row, alias, fetch_mode)?)?;
        }
        Ok(instances)
    }

    /// How many columns each row must have.
    #[getter]
    fn column_count(&self) -> usize {
        self.columns.len()
    }
}

impl InstanceBuilder {
    /// One instance from one row (see the module docs for what it does).
    fn build_one<'py>(
        &self,
        py: Python<'py>,
        row: &Bound<'py, PyTuple>,
        alias: &Bound<'py, PyString>,
        fetch_mode: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if row.len() != self.columns.len() {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "a row has {} values, but the model has {} columns",
                row.len(),
                self.columns.len()
            )));
        }
        // `.bind(py)` turns a stored Python object (`Py<...>`) into one we
        // can call while holding the GIL (`Bound<...>`).
        let instance = self.new_object.bind(py).call1((self.model.bind(py),))?;
        let state = self.model_state.bind(py).call0()?;
        // `intern!` makes each attribute name once and reuses it, like a module-level constant.
        state.setattr(intern!(py, "adding"), false)?;
        state.setattr(intern!(py, "db"), alias)?;
        if let Some(fetch_mode) = fetch_mode {
            state.setattr(intern!(py, "fetch_mode"), fetch_mode)?;
        }
        instance.setattr(intern!(py, "_state"), state)?;

        for (column, value) in self.columns.iter().zip(row.iter()) {
            let value = self.convert(py, column, value)?;
            // `setattr`, like Django's `__init__`, so a field's descriptor (if it has one) sees the value.
            instance.setattr(column.attname.bind(py), value)?;
        }
        Ok(instance)
    }

    /// One value of `column`, converted: in Rust if it can, else with Django's converters.
    fn convert<'py>(&self, py: Python<'py>, column: &Column, value: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        // `if let Some(x) = ...` runs the block only when there is a value, like
        // `if (x := ...) is not None:`; `&&` chains a second one.
        if let Some(conversion) = &column.conversion
            && let Some(converted) = conversion.convert(py, &value, &self.classes)?
        {
            return Ok(converted);
        }
        let mut value = value;
        for converter in &column.converters {
            value = converter
                .bind(py)
                .call1((value, column.expression.bind(py), self.connection.bind(py)))?;
        }
        Ok(value)
    }
}
