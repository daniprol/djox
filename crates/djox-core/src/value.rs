//! One value sent to or read from the database.

use chrono::{DateTime, NaiveDate, NaiveDateTime, NaiveTime, TimeDelta, Utc};
use uuid::Uuid;

/// A value in a query's parameters or in a result row.
///
/// The first five are SQLite's five storage classes, which every database
/// can map its own types to. SQLite stores everything as one of them, and
/// Django's field converters turn them back into dates, decimals and so on.
///
/// Postgres keeps its values typed, and Django's Postgres backend has no
/// converters: it expects the driver to hand back a `datetime`, a `Decimal`,
/// a `UUID`. So the Postgres backend reads its columns into the typed
/// variants below, and Python gets the same objects psycopg would give it.
/// SQLite never returns them.
///
/// An `enum` is a type that is exactly one of its variants, each with its own
/// data. In Python terms: `Value = None | int | float | str | bytes | bool | ...`.
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
    /// `True` or `False`. SQLite stores it as `1` or `0`.
    Boolean(bool),
    /// A Postgres `numeric`, as its digits in text (`"-12.50"`, `"NaN"`):
    /// Python's `Decimal` reads it without losing a digit.
    Decimal(String),
    /// A date (`date`).
    Date(NaiveDate),
    /// A time of day without a time zone (`time`).
    Time(NaiveTime),
    /// A date and time without a time zone (`timestamp`): a "naive" `datetime`.
    DateTime(NaiveDateTime),
    /// A moment in time (`timestamptz`), in UTC: an "aware" `datetime`.
    DateTimeUtc(DateTime<Utc>),
    /// A length of time (`interval`): a `timedelta`.
    Duration(TimeDelta),
    /// A `uuid`.
    Uuid(Uuid),
    /// A Postgres array (`integer[]`, `text[]`...): a Python `list`. An array
    /// of arrays (`integer[][]`) is a list of lists. Its items can be `Null`.
    List(Vec<Value>),
    /// A Postgres range (`int4range`, `daterange`, `tstzrange`...): a psycopg `Range`.
    /// It is boxed because a `Value` can't contain another `Value` directly (it
    /// would have no fixed size); `Box` keeps the range on the heap, like a Python object.
    Range(Box<RangeValue>),
    /// A Postgres `hstore` (the `hstore` extension's text-to-text map): a `dict`.
    /// A key with no value (`NULL` in the map) is `None`. The pairs keep Postgres's order.
    Hstore(Vec<(String, Option<String>)>),
}

/// The parts of a Postgres range.
///
/// `[1,5)` is the numbers from 1 up to, but not including, 5: the square
/// bracket includes its end, the round one leaves it out. A range can have
/// no lower or no upper end (`[1,)` goes on forever), or be empty.
#[derive(Debug, Clone, PartialEq)]
pub struct RangeValue {
    /// `None` when the range has no lower end.
    pub lower: Option<Value>,
    /// `None` when the range has no upper end.
    pub upper: Option<Value>,
    /// `true` for `[`, `false` for `(`.
    pub lower_is_included: bool,
    /// `true` for `]`, `false` for `)`.
    pub upper_is_included: bool,
    /// `true` for the range with no values at all (`'empty'`).
    pub is_empty: bool,
}
