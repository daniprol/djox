//! What a query or a write gives back.

use crate::Value;

/// The result of a query: the column names and every row, in order.
///
/// Rows are plain lists of values, like the tuples of Python's `sqlite3`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Rows {
    /// The name of each column, as the query named it.
    pub columns: Vec<String>,
    /// One `Vec<Value>` per row, with one value per column.
    pub rows: Vec<Vec<Value>>,
}

/// The result of a statement that changes data (`INSERT`, `UPDATE`, `DELETE`...).
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Executed {
    /// How many rows the statement inserted, changed or deleted.
    pub rows_affected: u64,
    /// The id of the last inserted row (SQLite's `rowid`), or 0 if none was inserted yet.
    pub last_insert_id: i64,
}
