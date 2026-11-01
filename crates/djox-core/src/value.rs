//! One value sent to or read from the database.

/// A value in a query's parameters or in a result row.
///
/// These are SQLite's five storage classes. Every database can map its own
/// types to them; a backend that needs more (Postgres arrays, say) can add
/// variants here later.
/// Richer Python types (`datetime`, `Decimal`, `UUID`...) are turned into one
/// of these on the way in, exactly like Django's own SQLite backend does, and
/// Django's field converters turn them back on the way out.
///
/// An `enum` is a type that is exactly one of its variants, each with its own
/// data. In Python terms: `Value = None | int | float | str | bytes`.
///
/// `#[derive(Debug, Clone, PartialEq)]` asks the compiler to write three
/// methods for us: printing for debugging (like `__repr__`), copying
/// (like `copy.deepcopy`) and comparing with `==` (like `__eq__`).
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Integer(i64),
    Real(f64),
    Text(String),
    Blob(Vec<u8>),
}
