//! `djox.ResultBuilder`: turns result rows into what a Django `QuerySet`
//! returns, for the fast lane: model instances, tuples (`values_list()`) or
//! dicts (`values()`).
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
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

use crate::conversions::{Conversion, PythonClasses};

/// Builds results from rows with the same columns.
///
/// Made once per query shape (`djox._fast_lane.statements`) and used for
/// every query of that shape. `frozen`: it never changes after it is made, so
/// any thread can use it.
#[pyclass(module = "djox", frozen)]
pub struct ResultBuilder {
    /// One column after another: its name, and how to convert it.
    columns: Vec<Column>,
    /// Django's `DatabaseWrapper`, which every converter receives.
    connection: Py<PyAny>,
    /// `datetime`, `Decimal`...: what the Rust conversions make.
    classes: PythonClasses,
    /// For `instances()`: the model class, like `Book` (`Option`: a class, or `None`).
    model: Option<Py<PyAny>>,
    /// For `instances()`: Django's `ModelState` class (every instance has one, as `_state`).
    model_state: Option<Py<PyAny>>,
    /// `object.__new__`, which makes an instance without calling `__init__`.
    new_object: Py<PyAny>,
}

/// What Python passes for one column's converters: `None`, or
/// `(functions, expression)`. (`type` gives a long type a short name, like a
/// type alias in Python.)
type ConvertersFromPython = Option<(Vec<Py<PyAny>>, Py<PyAny>)>;

/// One column of the rows.
struct Column {
    /// The attribute of an instance the value goes to, like `"author_id"`
    /// (Django's `attname`), or the key of a dict, like `"author"`. Stored
    /// as a Python string, so it isn't made again for every row.
    name: Py<PyString>,
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
impl ResultBuilder {
    /// `names` has the name of each column (see `Column::name`). `converters`
    /// has one entry per column: `None`, or `(functions, expression)`, the
    /// way Django's `SQLCompiler.get_converters()` describes them.
    /// `conversions` has one entry per column too: `None`, or the Rust
    /// conversion that does the same as the column's converters (see
    /// `conversions.rs`). `model` and `model_state` are needed for `instances()` only.
    ///
    /// `#[new]` makes this the Python constructor, `ResultBuilder(...)`.
    #[new]
    #[pyo3(signature = (names, converters, conversions, connection, model = None, model_state = None))]
    fn new(
        py: Python<'_>,
        names: Vec<String>,
        converters: Vec<ConvertersFromPython>,
        conversions: Vec<Option<Bound<'_, PyTuple>>>,
        connection: Py<PyAny>,
        model: Option<Py<PyAny>>,
        model_state: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        if names.len() != converters.len() || names.len() != conversions.len() {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "names, converters and conversions must have one entry per column",
            ));
        }
        let mut columns = Vec::with_capacity(names.len());
        // `zip` walks two lists side by side, like Python's `zip()`.
        for ((name, column_converters), description) in names.iter().zip(converters).zip(conversions) {
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
                name: PyString::intern(py, name).unbind(),
                converters: functions,
                expression,
                conversion,
            });
        }
        Ok(ResultBuilder {
            columns,
            connection,
            classes: PythonClasses::import(py)?,
            model,
            model_state,
            new_object: py.import("builtins")?.getattr("object")?.getattr("__new__")?.unbind(),
        })
    }

    /// One model instance per row, in order. `rows` is a list of tuples, as
    /// `fetch_all()` returns them; `alias` is the database alias the rows came
    /// from. `fetch_mode` (Django 6.1 and later) is set on each instance's state.
    #[pyo3(signature = (rows, alias, fetch_mode = None))]
    fn instances<'py>(
        &self,
        py: Python<'py>,
        rows: &Bound<'py, PyList>,
        alias: &Bound<'py, PyString>,
        fetch_mode: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyList>> {
        // `let (Some(a), Some(b)) = ... else` unpacks both, or leaves the function.
        let (Some(model), Some(model_state)) = (&self.model, &self.model_state) else {
            return Err(pyo3::exceptions::PyTypeError::new_err(
                "instances() needs a ResultBuilder made with a model and model_state",
            ));
        };
        let instances = PyList::empty(py);
        for row in rows.iter() {
            // `cast` checks the type, like `isinstance(row, tuple)`, and fails with a `TypeError`.
            let row = row.cast_into::<PyTuple>()?;
            self.check_length(&row)?;
            // `.bind(py)` turns a stored Python object (`Py<...>`) into one we
            // can call while holding the GIL (`Bound<...>`).
            let instance = self.new_object.bind(py).call1((model.bind(py),))?;
            let state = model_state.bind(py).call0()?;
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
                instance.setattr(column.name.bind(py), value)?;
            }
            instances.append(instance)?;
        }
        Ok(instances)
    }

    /// One tuple of converted values per row, like `values_list()` returns them.
    fn tuples<'py>(&self, py: Python<'py>, rows: &Bound<'py, PyList>) -> PyResult<Bound<'py, PyList>> {
        let tuples = PyList::empty(py);
        for row in rows.iter() {
            let row = row.cast_into::<PyTuple>()?;
            self.check_length(&row)?;
            let mut values = Vec::with_capacity(self.columns.len());
            for (column, value) in self.columns.iter().zip(row.iter()) {
                values.push(self.convert(py, column, value)?);
            }
            tuples.append(PyTuple::new(py, values)?)?;
        }
        Ok(tuples)
    }

    /// One dict per row, from each column's name to its converted value, like `values()` returns them.
    fn dicts<'py>(&self, py: Python<'py>, rows: &Bound<'py, PyList>) -> PyResult<Bound<'py, PyList>> {
        let dicts = PyList::empty(py);
        for row in rows.iter() {
            let row = row.cast_into::<PyTuple>()?;
            self.check_length(&row)?;
            let dict = PyDict::new(py);
            for (column, value) in self.columns.iter().zip(row.iter()) {
                dict.set_item(column.name.bind(py), self.convert(py, column, value)?)?;
            }
            dicts.append(dict)?;
        }
        Ok(dicts)
    }

    /// How many columns each row must have.
    #[getter]
    fn column_count(&self) -> usize {
        self.columns.len()
    }
}

impl ResultBuilder {
    /// An error if `row` doesn't have one value per column.
    fn check_length(&self, row: &Bound<'_, PyTuple>) -> PyResult<()> {
        if row.len() != self.columns.len() {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "a row has {} values, but there are {} columns",
                row.len(),
                self.columns.len()
            )));
        }
        Ok(())
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
