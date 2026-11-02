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
//! With `select_related("author")`, each row also holds the columns of the
//! book's author (Django joins the tables). The builder then makes the author
//! the same way, from its own columns, and links the two with Django's own
//! setters, like Django's `RelatedPopulator` does:
//!
//! ```python
//! author = make_instance(Author, row[7:9])   # the author's columns
//! set_on_parent(book, author)                # book.author is now cached
//! set_on_related(author, book)               # for a one-to-one, the way back
//! ```
//!
//! A nullable foreign key may have no author: its columns are all `NULL`
//! (a `LEFT OUTER JOIN`), and the book gets `None` instead.
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
    /// For `instances()`: the models `select_related()` adds to each
    /// instance, like a book's author. Empty without `select_related()`.
    related: Vec<Py<RelatedModel>>,
}

/// A model that `select_related()` adds to each instance, like the author of
/// a book: where its columns are in the row, and how to link it to the
/// instance it belongs to. Made in Python from what Django's compiler says
/// (`klass_info`, the same description Django's `RelatedPopulator` reads).
///
/// `frozen`: it never changes after it is made, so any thread can use it.
#[pyclass(module = "djox", frozen)]
pub struct RelatedModel {
    /// The model class, like `Author`.
    model: Py<PyAny>,
    /// The position of its first column in the row.
    first_column: usize,
    /// The position just after its last column (like `row[first:end]` in Python).
    end_column: usize,
    /// The position of its primary key in the row: `NULL` there means there
    /// is no related object (a nullable foreign key that is empty).
    primary_key_column: usize,
    /// Django's function that caches the related object on its parent, like
    /// `book.author`: called as `set_on_parent(book, author_or_none)`.
    set_on_parent: Py<PyAny>,
    /// Django's function that caches the parent on the related object, for
    /// a one-to-one: called as `set_on_related(author, book)`. For other
    /// relations Django's function does nothing.
    set_on_related: Py<PyAny>,
    /// The models `select_related()` adds to this one in turn, like the
    /// author's publisher in `select_related("author__publisher")`.
    related: Vec<Py<RelatedModel>>,
}

#[pymethods]
impl RelatedModel {
    /// The Python constructor, `RelatedModel(...)`; see the fields above.
    #[new]
    #[pyo3(signature = (model, first_column, end_column, primary_key_column, set_on_parent, set_on_related, related = Vec::new()))]
    fn new(
        model: Py<PyAny>,
        first_column: usize,
        end_column: usize,
        primary_key_column: usize,
        set_on_parent: Py<PyAny>,
        set_on_related: Py<PyAny>,
        related: Vec<Py<RelatedModel>>,
    ) -> PyResult<Self> {
        if first_column >= end_column || primary_key_column < first_column || primary_key_column >= end_column {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "a related model needs first_column <= primary_key_column < end_column",
            ));
        }
        Ok(RelatedModel {
            model,
            first_column,
            end_column,
            primary_key_column,
            set_on_parent,
            set_on_related,
            related,
        })
    }
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
    /// `related` lists the models `select_related()` adds (see `RelatedModel`):
    /// the model's own columns come first, and end where the first related
    /// model's columns begin.
    ///
    /// `#[new]` makes this the Python constructor, `ResultBuilder(...)`.
    #[new]
    #[pyo3(signature = (names, converters, conversions, connection, model = None, model_state = None, related = Vec::new()))]
    // Clippy (Rust's linter) counts the arguments; a Python constructor with
    // keyword arguments is clearer than a struct of options here.
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        names: Vec<String>,
        converters: Vec<ConvertersFromPython>,
        conversions: Vec<Option<Bound<'_, PyTuple>>>,
        connection: Py<PyAny>,
        model: Option<Py<PyAny>>,
        model_state: Option<Py<PyAny>>,
        related: Vec<Py<RelatedModel>>,
    ) -> PyResult<Self> {
        if names.len() != converters.len() || names.len() != conversions.len() {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "names, converters and conversions must have one entry per column",
            ));
        }
        if !related_columns_fit(&related, names.len()) {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "a related model's columns must be within the row's columns",
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
            related,
        })
    }

    /// One model instance per row, in order, with the objects of
    /// `select_related()` cached on it. `rows` is a list of tuples, as
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
        // `let Some(model) = ... else` unpacks it, or leaves the function.
        let Some(model) = &self.model else {
            return Err(pyo3::exceptions::PyTypeError::new_err(
                "instances() needs a ResultBuilder made with a model and model_state",
            ));
        };
        // The model's own columns end where the first related model's begin.
        // (`.first()` is an `Option`: the first related model, or `None`.)
        let model_end_column = match self.related.first() {
            Some(related) => related.get().first_column,
            None => self.columns.len(),
        };
        let instances = PyList::empty(py);
        for row in rows.iter() {
            // `cast` checks the type, like `isinstance(row, tuple)`, and fails with a `TypeError`.
            let row = row.cast_into::<PyTuple>()?;
            self.check_length(&row)?;
            // Every column converted first, like Django's `results_iter()`:
            // the related models read their columns from the same values.
            let values = self.converted_values(py, &row)?;
            // `&values[a..b]` is a slice, like `values[a:b]` in Python, without copying.
            let instance = self.new_instance(py, model.bind(py), &values[0..model_end_column], 0, alias, fetch_mode)?;
            for related in &self.related {
                // `.get()` reads a frozen Python object's Rust fields.
                self.add_related(py, related.get(), &instance, &values, alias, fetch_mode)?;
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
    /// A new instance of `model` from `values`, the converted values of its
    /// columns, which start at column `first_column` of the row. Made the
    /// way `__init__` and `from_db()` leave it (see the top of this file).
    fn new_instance<'py>(
        &self,
        py: Python<'py>,
        model: &Bound<'py, PyAny>,
        values: &[Bound<'py, PyAny>],
        first_column: usize,
        alias: &Bound<'py, PyString>,
        fetch_mode: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let Some(model_state) = &self.model_state else {
            return Err(pyo3::exceptions::PyTypeError::new_err(
                "instances() needs a ResultBuilder made with a model and model_state",
            ));
        };
        // `.bind(py)` turns a stored Python object (`Py<...>`) into one we
        // can call while holding the GIL (`Bound<...>`).
        let instance = self.new_object.bind(py).call1((model,))?;
        let state = model_state.bind(py).call0()?;
        // `intern!` makes each attribute name once and reuses it, like a module-level constant.
        state.setattr(intern!(py, "adding"), false)?;
        state.setattr(intern!(py, "db"), alias)?;
        // `if let Some(x) = ...` runs the block only when there is a value,
        // like `if (x := ...) is not None:`.
        if let Some(fetch_mode) = fetch_mode {
            state.setattr(intern!(py, "fetch_mode"), fetch_mode)?;
        }
        instance.setattr(intern!(py, "_state"), state)?;
        let columns = &self.columns[first_column..first_column + values.len()];
        for (column, value) in columns.iter().zip(values) {
            // `setattr`, like Django's `__init__`, so a field's descriptor (if it has one) sees the value.
            instance.setattr(column.name.bind(py), value)?;
        }
        Ok(instance)
    }

    /// Make the related object `related` describes from the row's `values`
    /// (and, in turn, its own related objects), and cache it on `parent`,
    /// like Django's `RelatedPopulator.populate()`.
    fn add_related<'py>(
        &self,
        py: Python<'py>,
        related: &RelatedModel,
        parent: &Bound<'py, PyAny>,
        values: &[Bound<'py, PyAny>],
        alias: &Bound<'py, PyString>,
        fetch_mode: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<()> {
        if values[related.primary_key_column].is_none() {
            // No related object: an empty nullable foreign key (or no row on
            // the other side of a one-to-one). `py.None()` is Python's `None`.
            related.set_on_parent.bind(py).call1((parent, py.None()))?;
            return Ok(());
        }
        let own_values = &values[related.first_column..related.end_column];
        let instance = self.new_instance(
            py,
            related.model.bind(py),
            own_values,
            related.first_column,
            alias,
            fetch_mode,
        )?;
        // Its own related objects first, then the links: the order Django uses.
        for child in &related.related {
            self.add_related(py, child.get(), &instance, values, alias, fetch_mode)?;
        }
        related.set_on_parent.bind(py).call1((parent, &instance))?;
        related.set_on_related.bind(py).call1((&instance, parent))?;
        Ok(())
    }

    /// Every value of `row`, converted (see `convert()`).
    fn converted_values<'py>(&self, py: Python<'py>, row: &Bound<'py, PyTuple>) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let mut values = Vec::with_capacity(self.columns.len());
        for (column, value) in self.columns.iter().zip(row.iter()) {
            values.push(self.convert(py, column, value)?);
        }
        Ok(values)
    }

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

/// Whether every related model (and theirs, in turn) has its columns within
/// the row's `column_count` columns, so `instances()` never reads past the end.
fn related_columns_fit(related: &[Py<RelatedModel>], column_count: usize) -> bool {
    for related_model in related {
        let related_model = related_model.get();
        if related_model.end_column > column_count || !related_columns_fit(&related_model.related, column_count) {
            return false;
        }
    }
    true
}
