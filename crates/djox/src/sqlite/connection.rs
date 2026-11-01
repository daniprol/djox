//! One SQLite connection: opening it with the right settings, running
//! statements, and converting values and errors.
//!
//! A connection is used by one thread at a time (its worker), and every
//! method here blocks until SQLite is done. That is fine: workers are threads
//! made for waiting, and Python's event loop never calls these directly.

use djox_core::{Error, Executed, Result, Rows, Value};
use rusqlite::types::{ToSqlOutput, ValueRef};
use rusqlite::{ErrorCode, OpenFlags, TransactionBehavior, params_from_iter};

use super::placeholders::to_question_marks;
use super::settings::SqliteSettings;

/// What a connection is for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Access {
    /// The one connection that writes.
    Writer,
    /// A connection opened read-only: SQLite itself refuses any write on it.
    Reader,
}

/// One open SQLite connection.
///
/// `pub(crate)` means "public inside this crate only": the rest of djox can
/// use it, users of the crate can't (like a leading underscore in Python,
/// but enforced by the compiler).
pub(crate) struct SqliteConnection {
    connection: rusqlite::Connection,
}

impl SqliteConnection {
    /// Opens a connection to the settings' file.
    ///
    /// The writer creates the file if needed and sets it up:
    ///
    /// - `journal_mode = WAL`: readers and the writer don't block each other.
    ///   It is stored in the file, so Django's own connections use it too.
    /// - `synchronous = NORMAL`: safe with WAL and much faster than `FULL`. A
    ///   power cut can lose the last commits but never corrupts the file.
    /// - `foreign_keys = ON`: like Django, so foreign keys are checked.
    ///
    /// Every connection waits up to `busy_timeout` for a lock held by another
    /// process before failing.
    pub(crate) fn open(settings: &SqliteSettings, access: Access) -> Result<SqliteConnection> {
        // `NO_MUTEX`: only one thread uses a connection at a time, so SQLite
        // can skip its own locking. `URI`: accept `file:...` paths.
        let shared_flags = OpenFlags::SQLITE_OPEN_URI | OpenFlags::SQLITE_OPEN_NO_MUTEX;
        let flags = match access {
            Access::Writer => shared_flags | OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
            Access::Reader => shared_flags | OpenFlags::SQLITE_OPEN_READ_ONLY,
        };
        let connection = rusqlite::Connection::open_with_flags(&settings.path, flags).map_err(to_djox_error)?;
        connection.busy_timeout(settings.busy_timeout).map_err(to_djox_error)?;
        // Compiled statements are kept and reused, keyed by their SQL (see `prepare_cached`).
        connection.set_prepared_statement_cache_capacity(256);

        if access == Access::Writer {
            // This PRAGMA answers with the mode it ended up in, so we read it like a query.
            let journal_mode: String = connection
                .query_row("PRAGMA journal_mode = WAL", [], |row| row.get(0))
                .map_err(to_djox_error)?;
            if !journal_mode.eq_ignore_ascii_case("wal") {
                return Err(Error::Settings(format!(
                    "could not turn on WAL mode (SQLite answered {journal_mode:?})"
                )));
            }
            connection
                .execute_batch("PRAGMA synchronous = NORMAL; PRAGMA foreign_keys = ON;")
                .map_err(to_djox_error)?;
        }
        Ok(SqliteConnection { connection })
    }

    /// Whether the statement changes the database (SQLite tells us, without
    /// running it). `SELECT` doesn't; `INSERT ... RETURNING` does.
    pub(crate) fn statement_writes(&mut self, sql: &str) -> Result<bool> {
        let sql = to_question_marks(sql);
        let statement = self.connection.prepare_cached(&sql).map_err(to_djox_error)?;
        Ok(!statement.readonly())
    }

    /// Runs a statement and returns all its rows.
    pub(crate) fn query(&mut self, sql: &str, parameters: &[Value]) -> Result<Rows> {
        let sql = to_question_marks(sql);
        // `prepare_cached` compiles the SQL the first time and reuses it after.
        // Compiling is often slower than running a small query.
        let mut statement = self.connection.prepare_cached(&sql).map_err(to_djox_error)?;
        let columns: Vec<String> = statement.column_names().map(String::from).collect();
        let column_count = columns.len();

        let mut rows = Vec::new();
        let mut cursor = statement
            .query(params_from_iter(parameters.iter().map(to_sqlite)))
            .map_err(to_djox_error)?;
        // `while let Some(row) = ...` loops until `next()` returns `None`: no more rows.
        while let Some(row) = cursor.next().map_err(to_djox_error)? {
            let mut values = Vec::with_capacity(column_count);
            for index in 0..column_count {
                values.push(from_sqlite(row.get_ref(index).map_err(to_djox_error)?));
            }
            rows.push(values);
        }
        Ok(Rows { columns, rows })
    }

    /// Runs a statement that changes data.
    pub(crate) fn execute(&mut self, sql: &str, parameters: &[Value]) -> Result<Executed> {
        let sql = to_question_marks(sql);
        let rows_affected = {
            // The braces end the statement's borrow of the connection, so we
            // can ask the connection for the last inserted id afterwards.
            let mut statement = self.connection.prepare_cached(&sql).map_err(to_djox_error)?;
            statement
                .execute(params_from_iter(parameters.iter().map(to_sqlite)))
                .map_err(to_djox_error)?
        };
        Ok(Executed {
            rows_affected: rows_affected as u64,
            last_insert_id: self.connection.last_insert_rowid(),
        })
    }

    /// Runs a statement once per list of parameters, in one transaction.
    pub(crate) fn execute_many(&mut self, sql: &str, parameter_lists: &[Vec<Value>]) -> Result<Executed> {
        let sql = to_question_marks(sql);
        // `BEGIN IMMEDIATE` takes the write lock right away, instead of
        // upgrading a read lock later (which can fail with "database is locked").
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(to_djox_error)?;
        let mut rows_affected = 0;
        {
            let mut statement = transaction.prepare_cached(&sql).map_err(to_djox_error)?;
            for parameters in parameter_lists {
                rows_affected += statement
                    .execute(params_from_iter(parameters.iter().map(to_sqlite)))
                    .map_err(to_djox_error)?;
            }
        }
        // If anything above failed, `?` already returned, and dropping the
        // unfinished `transaction` rolled it back (like `with transaction:` in Python).
        transaction.commit().map_err(to_djox_error)?;
        Ok(Executed {
            rows_affected: rows_affected as u64,
            last_insert_id: self.connection.last_insert_rowid(),
        })
    }
}

/// A djox value as a SQLite parameter. It borrows the text or bytes (`&`)
/// instead of copying them.
fn to_sqlite(value: &Value) -> ToSqlOutput<'_> {
    let borrowed = match value {
        Value::Null => ValueRef::Null,
        // `*number` reads the number behind the reference (numbers are cheap to copy).
        Value::Integer(number) => ValueRef::Integer(*number),
        Value::Real(number) => ValueRef::Real(*number),
        Value::Text(text) => ValueRef::Text(text.as_bytes()),
        Value::Blob(bytes) => ValueRef::Blob(bytes),
    };
    ToSqlOutput::Borrowed(borrowed)
}

/// A SQLite value as a djox value (this one copies, since the row it comes
/// from is gone after the next `cursor.next()`).
fn from_sqlite(value: ValueRef<'_>) -> Value {
    match value {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(number) => Value::Integer(number),
        ValueRef::Real(number) => Value::Real(number),
        // SQLite text should be UTF-8; invalid bytes become "�" instead of failing the whole query.
        ValueRef::Text(bytes) => Value::Text(String::from_utf8_lossy(bytes).into_owned()),
        ValueRef::Blob(bytes) => Value::Blob(bytes.to_vec()),
    }
}

/// Sorts a SQLite error into a djox error, the way Python's `sqlite3` module
/// does: failed constraints are integrity errors; anything else SQLite
/// reports (a syntax error, a missing table, a lock timeout) is operational.
pub(crate) fn to_djox_error(error: rusqlite::Error) -> Error {
    let message = error.to_string();
    match error.sqlite_error_code() {
        Some(ErrorCode::ConstraintViolation) => Error::Integrity(message),
        Some(_) => Error::Operational(message),
        // Errors found by rusqlite itself, before or after SQLite ran.
        None => match error {
            // A syntax error, found while compiling the statement.
            rusqlite::Error::SqlInputError { .. } => Error::Operational(message),
            rusqlite::Error::ExecuteReturnedResults => {
                Error::Database("this statement returns rows: use fetch_all() instead of execute()".to_string())
            }
            _ => Error::Database(message),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A writer connection to a new database with a `book` table.
    /// The folder is returned too: the files are deleted when it is dropped.
    fn new_writer() -> (tempfile::TempDir, SqliteConnection) {
        let folder = tempfile::tempdir().unwrap();
        let settings = SqliteSettings::new(folder.path().join("app.sqlite3"));
        let mut writer = SqliteConnection::open(&settings, Access::Writer).unwrap();
        writer
            .execute(
                "CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT UNIQUE NOT NULL, rating REAL, cover BLOB)",
                &[],
            )
            .unwrap();
        (folder, writer)
    }

    #[test]
    fn writer_turns_on_wal_and_readers_cannot_write() {
        let (folder, mut writer) = new_writer();
        let journal_mode = writer.query("PRAGMA journal_mode", &[]).unwrap();
        assert_eq!(journal_mode.rows[0][0], Value::Text("wal".to_string()));

        let settings = SqliteSettings::new(folder.path().join("app.sqlite3"));
        let mut reader = SqliteConnection::open(&settings, Access::Reader).unwrap();
        assert!(!reader.statement_writes("SELECT count(*) FROM book").unwrap());
        assert!(
            reader
                .statement_writes("INSERT INTO book (title) VALUES ('Dune') RETURNING id")
                .unwrap()
        );
        let refused = reader.execute("INSERT INTO book (title) VALUES ('Dune')", &[]);
        assert!(matches!(refused, Err(Error::Operational(message)) if message.contains("readonly")));
    }

    #[test]
    fn values_go_in_and_come_out_unchanged() {
        let (_folder, mut writer) = new_writer();
        let inserted = writer
            .execute(
                "INSERT INTO book (title, rating, cover) VALUES (%s, %s, %s)",
                &[Value::Text("Dune".into()), Value::Real(4.5), Value::Blob(vec![0, 255])],
            )
            .unwrap();
        assert_eq!(
            inserted,
            Executed {
                rows_affected: 1,
                last_insert_id: 1
            }
        );

        let rows = writer
            .query("SELECT id, title, rating, cover, NULL AS empty FROM book", &[])
            .unwrap();
        assert_eq!(rows.columns, ["id", "title", "rating", "cover", "empty"]);
        assert_eq!(
            rows.rows,
            [[
                Value::Integer(1),
                Value::Text("Dune".into()),
                Value::Real(4.5),
                Value::Blob(vec![0, 255]),
                Value::Null
            ]]
        );
    }

    #[test]
    fn execute_many_is_all_or_nothing() {
        let (_folder, mut writer) = new_writer();
        let sql = "INSERT INTO book (title) VALUES (%s)";
        let written = writer
            .execute_many(sql, &[vec![Value::Text("A".into())], vec![Value::Text("B".into())]])
            .unwrap();
        assert_eq!(written.rows_affected, 2);

        // "C" is new but "A" is a duplicate, so neither is written.
        let failed = writer.execute_many(sql, &[vec![Value::Text("C".into())], vec![Value::Text("A".into())]]);
        assert!(matches!(failed, Err(Error::Integrity(_))));
        let count = writer.query("SELECT count(*) FROM book", &[]).unwrap();
        assert_eq!(count.rows[0][0], Value::Integer(2));
    }

    #[test]
    fn errors_are_sorted_like_pythons_sqlite3() {
        let (_folder, mut writer) = new_writer();
        let missing_title = writer.execute("INSERT INTO book (rating) VALUES (1)", &[]);
        assert!(matches!(missing_title, Err(Error::Integrity(_))));
        let missing_table = writer.query("SELECT * FROM author", &[]);
        assert!(matches!(missing_table, Err(Error::Operational(message)) if message.contains("no such table")));
        let syntax_error = writer.query("SELEKT 1", &[]);
        assert!(matches!(syntax_error, Err(Error::Operational(_))));
        let returns_rows = writer.execute("INSERT INTO book (title) VALUES ('X') RETURNING id", &[]);
        assert!(matches!(returns_rows, Err(Error::Database(message)) if message.contains("fetch_all")));
    }
}
