//! SQL functions written outside SQLite, in Rust or (through the Python
//! extension) in Python.
//!
//! SQLite has no `EXTRACT(YEAR FROM ...)`, no `STDDEV`, no `MD5`... Django
//! fills the gaps by registering about 40 Python functions on every SQLite
//! connection (`django_date_extract`, `STDDEV_POP`...), and the SQL it writes
//! calls them. So every djox connection must be able to register them too.
//!
//! There are two kinds, like in Python's `sqlite3` module:
//!
//! - a **scalar function** takes the values of one row and returns one value
//!   (`create_function()` in Python);
//! - an **aggregate** sees every row of a group, one at a time, and returns one
//!   value at the end, like `SUM` (`create_aggregate()` in Python).
//!
//! This crate knows nothing about Python: a function is any Rust closure, and
//! an aggregate is any type with a `step` and a `finish` method.

use std::panic::AssertUnwindSafe;

use djox_core::{Error, Result, Value};
use rusqlite::functions::{Aggregate, Context, FunctionFlags};

use super::connection::{SqliteConnection, from_sqlite, to_djox_error};

/// The running state of one aggregate, for one group of rows.
///
/// In Python's `sqlite3` an aggregate is a class with `step()` and
/// `finalize()`; a new instance is made for each group. This trait is the
/// same idea (a *trait* is like a Python `Protocol`).
pub trait AggregateState: Send {
    /// Called once per row of the group, with that row's arguments.
    fn step(&mut self, arguments: Vec<Value>) -> Result<()>;
    /// Called once at the end of the group: the aggregate's result.
    fn finish(&mut self) -> Result<Value>;
}

/// Makes a fresh [`AggregateState`] for each group of rows.
///
/// `Box<dyn AggregateState>` is "some value that implements the trait",
/// stored on the heap, since each aggregate has its own type.
pub type NewAggregateState = Box<dyn Fn() -> Result<Box<dyn AggregateState>> + Send>;

impl SqliteConnection {
    /// Registers a scalar function. `argument_count` -1 means "any number".
    ///
    /// A *deterministic* function always gives the same result for the same
    /// arguments, which lets SQLite use it in indexes and skip repeated calls.
    pub fn create_scalar_function(
        &mut self,
        name: &str,
        argument_count: i32,
        deterministic: bool,
        function: impl Fn(Vec<Value>) -> Result<Value> + Send + 'static,
    ) -> Result<()> {
        // `move` hands `function` over to the closure SQLite keeps.
        let call_from_sqlite = move |context: &Context<'_>| -> rusqlite::Result<rusqlite::types::Value> {
            let result = function(arguments_of(context));
            result.map(to_sqlite_value).map_err(to_function_error)
        };
        self.connection
            .create_scalar_function(name, argument_count, flags(deterministic), call_from_sqlite)
            .map_err(to_djox_error)
    }

    /// Registers an aggregate. `new_state` is called once per group of rows.
    pub fn create_aggregate_function(
        &mut self,
        name: &str,
        argument_count: i32,
        new_state: NewAggregateState,
    ) -> Result<()> {
        // Like Python's `sqlite3`, aggregates are not marked deterministic.
        self.connection
            .create_aggregate_function(name, argument_count, flags(false), AggregateAdapter { new_state })
            .map_err(to_djox_error)
    }
}

/// The flags SQLite wants for a function: text is UTF-8, and maybe deterministic.
fn flags(deterministic: bool) -> FunctionFlags {
    if deterministic {
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC
    } else {
        FunctionFlags::SQLITE_UTF8
    }
}

/// The arguments SQLite passed to a function, as djox values.
fn arguments_of(context: &Context<'_>) -> Vec<Value> {
    let mut arguments = Vec::with_capacity(context.len());
    for index in 0..context.len() {
        arguments.push(from_sqlite(context.get_raw(index)));
    }
    arguments
}

/// A djox value as a value SQLite owns (the function's result).
fn to_sqlite_value(value: Value) -> rusqlite::types::Value {
    match value {
        Value::Null => rusqlite::types::Value::Null,
        Value::Integer(number) => rusqlite::types::Value::Integer(number),
        Value::Real(number) => rusqlite::types::Value::Real(number),
        Value::Text(text) => rusqlite::types::Value::Text(text),
        Value::Blob(bytes) => rusqlite::types::Value::Blob(bytes),
    }
}

/// A failed function becomes a failed statement. SQLite reports it as an
/// ordinary error, which djox sorts as an `OperationalError`, like Python's
/// `sqlite3` does ("user-defined function raised exception").
fn to_function_error(error: Error) -> rusqlite::Error {
    rusqlite::Error::UserFunctionError(Box::new(error))
}

/// Connects an [`AggregateState`] to rusqlite's own `Aggregate` trait.
struct AggregateAdapter {
    new_state: NewAggregateState,
}

/// rusqlite wants the state to be "unwind safe" (safe to keep using after a
/// Rust panic). Ours is dropped on any error, so `AssertUnwindSafe` just tells
/// the compiler so.
type SafeState = AssertUnwindSafe<Box<dyn AggregateState>>;

impl Aggregate<SafeState, rusqlite::types::Value> for AggregateAdapter {
    /// Called before the first row of each group.
    fn init(&self, _context: &mut Context<'_>) -> rusqlite::Result<SafeState> {
        let state = (self.new_state)().map_err(to_function_error)?;
        Ok(AssertUnwindSafe(state))
    }

    fn step(&self, context: &mut Context<'_>, state: &mut SafeState) -> rusqlite::Result<()> {
        state.step(arguments_of(context)).map_err(to_function_error)
    }

    /// `state` is `None` when the group had no rows: then the result is
    /// `NULL`, like Python's `sqlite3` (and SQL's own `SUM`).
    fn finalize(
        &self,
        _context: &mut Context<'_>,
        state: Option<SafeState>,
    ) -> rusqlite::Result<rusqlite::types::Value> {
        match state {
            Some(mut state) => state.finish().map(to_sqlite_value).map_err(to_function_error),
            None => Ok(rusqlite::types::Value::Null),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sqlite::{Access, SqliteSettings};

    fn new_connection() -> (tempfile::TempDir, SqliteConnection) {
        let folder = tempfile::tempdir().unwrap();
        let settings = SqliteSettings::new(folder.path().join("app.sqlite3"));
        (folder, SqliteConnection::open(&settings, Access::Writer).unwrap())
    }

    fn select_one(connection: &mut SqliteConnection, sql: &str) -> Result<Value> {
        Ok(connection.run(sql, &[])?.rows.rows[0][0].clone())
    }

    /// Adds up its integer arguments, like `SUM`.
    struct Total {
        sum: i64,
    }

    impl AggregateState for Total {
        fn step(&mut self, arguments: Vec<Value>) -> Result<()> {
            if let Value::Integer(number) = arguments[0] {
                self.sum += number;
            }
            Ok(())
        }

        fn finish(&mut self) -> Result<Value> {
            Ok(Value::Integer(self.sum))
        }
    }

    #[test]
    fn scalar_functions_are_called_with_each_rows_values() {
        let (_folder, mut connection) = new_connection();
        connection
            .create_scalar_function("shout", 1, true, |arguments| match &arguments[0] {
                Value::Text(text) => Ok(Value::Text(text.to_uppercase())),
                _ => Ok(Value::Null),
            })
            .unwrap();
        assert_eq!(
            select_one(&mut connection, "SELECT shout('dune')").unwrap(),
            Value::Text("DUNE".into())
        );
        assert_eq!(select_one(&mut connection, "SELECT shout(NULL)").unwrap(), Value::Null);
    }

    #[test]
    fn a_failing_function_fails_the_statement() {
        let (_folder, mut connection) = new_connection();
        connection
            .create_scalar_function("broken", 0, false, |_| Err(Error::Database("it broke".into())))
            .unwrap();
        let failed = select_one(&mut connection, "SELECT broken()");
        assert!(matches!(failed, Err(Error::Operational(message)) if message.contains("it broke")));
    }

    #[test]
    fn aggregates_see_every_row_of_a_group() {
        let (_folder, mut connection) = new_connection();
        let new_total: NewAggregateState = Box::new(|| Ok(Box::new(Total { sum: 0 })));
        connection.create_aggregate_function("total", 1, new_total).unwrap();
        connection.run("CREATE TABLE number (value INTEGER)", &[]).unwrap();
        connection.run("INSERT INTO number VALUES (1), (2), (39)", &[]).unwrap();

        assert_eq!(
            select_one(&mut connection, "SELECT total(value) FROM number").unwrap(),
            Value::Integer(42)
        );
        let no_rows = select_one(&mut connection, "SELECT total(value) FROM number WHERE value > 100");
        assert_eq!(no_rows.unwrap(), Value::Null);
    }

    #[test]
    fn math_functions_are_built_in() {
        let (_folder, mut connection) = new_connection();
        assert_eq!(
            select_one(&mut connection, "SELECT POWER(2, 10)").unwrap(),
            Value::Real(1024.0)
        );
    }
}
