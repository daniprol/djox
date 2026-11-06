//! One SQLite connection: opening it with the right settings, running
//! statements, and converting values and errors.
//!
//! A connection is used by one thread at a time, and every method here blocks
//! until SQLite is done. The async [`super::SqliteDatabase`] keeps each of its
//! connections on a worker thread made for waiting; the Python extension's
//! `djox.Connection` (used by Django's sync ORM) calls these methods on the
//! caller's thread, after letting other Python threads run.

use std::os::raw::c_int;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use chrono::Timelike; // `.nanosecond()` on times and datetimes
use djox_core::{ChunkRequests, Error, Executed, Result, Rows, Value};
use rusqlite::limits::Limit;
use rusqlite::types::{ToSqlOutput, ValueRef};
use rusqlite::{ErrorCode, OpenFlags, TransactionBehavior, params_from_iter};

use super::placeholders::to_question_marks;
use super::settings::{SqliteSettings, is_in_memory};

/// How often SQLite asks [`SqliteConnection::query_within`] whether time is
/// up: every this many steps of its virtual machine (a few to a few dozen
/// microseconds), so checking the clock costs next to nothing.
const STEPS_BETWEEN_CLOCK_CHECKS: i32 = 1000;

/// What a connection is for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Access {
    /// A connection that can write. The async database has exactly one; each
    /// of Django's sync connections is one too.
    Writer,
    /// A connection opened read-only: SQLite itself refuses any write on it.
    Reader,
}

/// How many pages SQLite's write-ahead log may grow to before it is copied
/// back into the database file (a *checkpoint*). It is SQLite's own default.
const PAGES_BEFORE_A_CHECKPOINT: c_int = 1000;

/// One open SQLite connection.
pub struct SqliteConnection {
    /// `pub(super)` means "visible to the other files of the `sqlite` folder"
    /// (`functions.rs` registers SQL functions on it), but not outside it.
    pub(super) connection: rusqlite::Connection,
    /// How long the connection waits for a lock held by another connection.
    busy_timeout: Duration,
}

/// What a statement run with [`SqliteConnection::run_without_waiting`] came to.
pub enum QuickOutcome {
    /// It ran: here is its output, or the error it failed with.
    Ran(Result<StatementOutput>),
    /// It didn't run to the end: it would have had to wait for a lock, or it
    /// ran out of time and SQLite stopped it. Nothing was changed; run it
    /// again the usual way.
    GaveUp,
}

/// What a statement run with [`SqliteConnection::run`] gave back.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct StatementOutput {
    /// The rows, if the statement returns any (`SELECT`, `PRAGMA`, `... RETURNING`).
    pub rows: Rows,
    /// How many rows an `INSERT`, `UPDATE`, `DELETE` or `REPLACE` changed, or
    /// `None` for any other statement (Python's DB-API calls it `rowcount`, and uses -1 for "none").
    pub rows_affected: Option<u64>,
    /// The id of the last row this connection inserted (SQLite's `rowid`).
    pub last_insert_id: i64,
}

impl SqliteConnection {
    /// Opens a connection to the settings' file.
    ///
    /// A writer creates the file if needed and sets it up:
    ///
    /// - `journal_mode = WAL`: readers and the writer don't block each other.
    ///   It is stored in the file, so every later connection uses it too.
    ///   (In-memory databases can't use WAL; they keep SQLite's default.)
    /// - `synchronous = NORMAL`: safe with WAL and much faster than `FULL`. A
    ///   power cut can lose the last commits but never corrupts the file.
    /// - `foreign_keys = ON`: like Django, so foreign keys are checked.
    ///
    /// Every connection waits up to `busy_timeout` for a lock held by another
    /// connection before failing.
    pub fn open(settings: &SqliteSettings, access: Access) -> Result<SqliteConnection> {
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
            if !is_in_memory(&settings.path.to_string_lossy()) {
                // This PRAGMA answers with the mode it ended up in, so we read it like a query.
                let journal_mode: String = connection
                    .query_row("PRAGMA journal_mode = WAL", [], |row| row.get(0))
                    .map_err(to_djox_error)?;
                if !journal_mode.eq_ignore_ascii_case("wal") {
                    return Err(Error::Settings(format!(
                        "could not turn on WAL mode (SQLite answered {journal_mode:?})"
                    )));
                }
            }
            connection
                .execute_batch("PRAGMA synchronous = NORMAL; PRAGMA foreign_keys = ON;")
                .map_err(to_djox_error)?;
        }
        Ok(SqliteConnection {
            connection,
            busy_timeout: settings.busy_timeout,
        })
    }

    /// Runs any one statement and returns what it gave back: rows, the number
    /// of rows it changed, the last inserted id. This is what a DB-API
    /// `cursor.execute()` needs, since it can't know in advance whether a
    /// statement returns rows.
    ///
    /// Unlike the other methods, `sql` must already use SQLite's own `?`
    /// placeholders (see `placeholders.rs`): Django runs SQL without
    /// parameters exactly as written, so the caller decides whether to convert.
    pub fn run(&mut self, sql: &str, parameters: &[Value]) -> Result<StatementOutput> {
        let rows = self.rows_of(sql, parameters)?;
        Ok(StatementOutput {
            rows,
            rows_affected: self.rows_affected_by(sql),
            last_insert_id: self.connection.last_insert_rowid(),
        })
    }

    /// Runs the same statement once per list of parameters, like a DB-API
    /// `cursor.executemany()`. Unlike [`SqliteConnection::execute_many`], it
    /// doesn't open a transaction of its own: Django's `atomic()` decides that.
    ///
    /// Only statements that change rows are allowed (like Python's `sqlite3`),
    /// since there would be no way to return the rows of each run.
    pub fn run_many(&mut self, sql: &str, parameter_lists: &[Vec<Value>]) -> Result<StatementOutput> {
        if !is_data_change(sql) {
            return Err(Error::Programming(
                "executemany() can only run INSERT, UPDATE, DELETE or REPLACE statements".to_string(),
            ));
        }
        let mut rows_affected = 0;
        for parameters in parameter_lists {
            self.rows_of(sql, parameters)?;
            rows_affected += self.connection.changes();
        }
        Ok(StatementOutput {
            rows: Rows::default(),
            rows_affected: Some(rows_affected),
            last_insert_id: self.connection.last_insert_rowid(),
        })
    }

    /// Whether a transaction is open (a `BEGIN` without its `COMMIT` or `ROLLBACK` yet).
    pub fn in_transaction(&self) -> bool {
        // SQLite calls "no transaction open" *autocommit* mode: each statement commits on its own.
        !self.connection.is_autocommit()
    }

    /// One of SQLite's limits, by its number (`SQLITE_LIMIT_VARIABLE_NUMBER`
    /// is 9: the most `?` a statement may have). Django reads two of them.
    pub fn limit(&self, category: i32) -> Result<i32> {
        let Some(limit) = limit_from_number(category) else {
            return Err(Error::Programming(format!("{category} is not a SQLite limit category")));
        };
        self.connection.limit(limit).map_err(to_djox_error)
    }

    /// Changes one of SQLite's limits for this connection, and returns its old
    /// value. SQLite won't raise a limit above the maximum it was built with.
    pub fn set_limit(&mut self, category: i32, value: i32) -> Result<i32> {
        let Some(limit) = limit_from_number(category) else {
            return Err(Error::Programming(format!("{category} is not a SQLite limit category")));
        };
        self.connection.set_limit(limit, value).map_err(to_djox_error)
    }

    /// Runs a statement in SQLite's `?` style and collects its rows.
    fn rows_of(&mut self, sql: &str, parameters: &[Value]) -> Result<Rows> {
        self.sqlite_rows_of(sql, parameters).map_err(to_djox_error)
    }

    /// Like [`SqliteConnection::rows_of`], with SQLite's own error, which
    /// tells why a statement stopped ([`SqliteConnection::run_without_waiting`] needs that).
    fn sqlite_rows_of(&mut self, sql: &str, parameters: &[Value]) -> rusqlite::Result<Rows> {
        // `prepare_cached` compiles the SQL the first time and reuses it after.
        // Compiling is often slower than running a small query.
        let mut statement = self.connection.prepare_cached(sql)?;
        let columns: Vec<String> = statement.column_names().map(String::from).collect();
        let column_count = columns.len();

        let mut rows = Vec::new();
        let mut cursor = statement.query(params_from_iter(parameters.iter().map(to_sqlite)))?;
        // `while let Some(row) = ...` loops until `next()` returns `None`: no more rows.
        while let Some(row) = cursor.next()? {
            let mut values = Vec::with_capacity(column_count);
            for index in 0..column_count {
                values.push(from_sqlite(row.get_ref(index)?));
            }
            rows.push(values);
        }
        Ok(Rows { columns, rows })
    }

    /// Runs a query written with Django's `%s` placeholders and answers each
    /// chunk `requests` asks for with the next `chunk_size` rows, until there
    /// are no more rows or the caller stops asking (see `djox_core::RowStream`).
    ///
    /// It returns when the stream is over: the statement is finished then.
    pub fn stream(&mut self, sql: &str, parameters: &[Value], chunk_size: usize, requests: &ChunkRequests) {
        // Nothing runs until the first chunk is asked for.
        let Some(mut reply) = requests.next() else { return };
        let sql = to_question_marks(sql);
        // `prepare` rather than `prepare_cached`: a stream's connection is used once.
        let mut statement = match self.connection.prepare(&sql) {
            Ok(statement) => statement,
            Err(error) => return reply.send(Err(to_djox_error(error))),
        };
        if !statement.readonly() {
            return reply.send(Err(Error::Programming(
                "only a query that reads can be streamed".to_string(),
            )));
        }
        let columns: Vec<String> = statement.column_names().map(String::from).collect();
        let column_count = columns.len();
        let mut cursor = match statement.query(params_from_iter(parameters.iter().map(to_sqlite))) {
            Ok(cursor) => cursor,
            Err(error) => return reply.send(Err(to_djox_error(error))),
        };
        loop {
            let mut chunk = Vec::with_capacity(chunk_size);
            let mut finished = false;
            while chunk.len() < chunk_size {
                // `match` on the result of `next()`: a row, the end, or an error.
                match cursor.next() {
                    Ok(Some(row)) => match values_of(row, column_count) {
                        Ok(values) => chunk.push(values),
                        Err(error) => return reply.send(Err(error)),
                    },
                    Ok(None) => {
                        finished = true;
                        break;
                    }
                    Err(error) => return reply.send(Err(to_djox_error(error))),
                }
            }
            reply.send(Ok(Rows {
                columns: columns.clone(),
                rows: chunk,
            }));
            if finished {
                return;
            }
            match requests.next() {
                Some(next_reply) => reply = next_reply,
                None => return, // the caller stopped early
            }
        }
    }

    /// How many rows the statement that just ran changed, if it is one that changes rows.
    fn rows_affected_by(&self, sql: &str) -> Option<u64> {
        if is_data_change(sql) {
            // `changes()` keeps the count of the last INSERT, UPDATE or DELETE,
            // even after other statements, so only ask after one of those.
            Some(self.connection.changes())
        } else {
            None
        }
    }

    /// Whether the statement changes the database (SQLite tells us, without
    /// running it). `SELECT` doesn't; `INSERT ... RETURNING` does.
    pub fn statement_writes(&mut self, sql: &str) -> Result<bool> {
        let sql = to_question_marks(sql);
        let statement = self.connection.prepare_cached(&sql).map_err(to_djox_error)?;
        Ok(!statement.readonly())
    }

    /// Runs a statement written with Django's `%s` placeholders and returns all its rows.
    pub fn query(&mut self, sql: &str, parameters: &[Value]) -> Result<Rows> {
        self.rows_of(&to_question_marks(sql), parameters)
    }

    /// Runs a query that reads, written with Django's `%s` placeholders, if
    /// SQLite can answer it within `time_limit` without waiting for a lock.
    /// `None` means it didn't: the statement writes, it would have taken
    /// longer (SQLite stopped it), or it failed. Then nothing was changed, and
    /// the caller runs it again the usual way, which also reports any error.
    pub fn query_within(&mut self, sql: &str, parameters: &[Value], time_limit: Duration) -> Option<Rows> {
        if self.statement_writes(sql) != Ok(false) {
            return None;
        }
        let deadline = Instant::now() + time_limit;
        // SQLite calls this function every `STEPS_BETWEEN_CLOCK_CHECKS` steps of
        // the statement; answering `true` stops the statement. `move` gives the
        // function its own copy of `deadline`.
        let stop_after_deadline = move || Instant::now() >= deadline;
        self.connection
            .progress_handler(STEPS_BETWEEN_CLOCK_CHECKS, Some(stop_after_deadline))
            .ok()?;
        // `.ok()` turns an error into `None`: "it failed, run it the usual way".
        let rows = self.query(sql, parameters).ok();
        // `None::<fn() -> bool>`: no function any more (Rust needs to be told
        // the type of the function that isn't there).
        let _ = self.connection.progress_handler(0, None::<fn() -> bool>);
        rows
    }

    /// Never waits for a lock held by another connection: a statement that
    /// would have to wait fails at once ("database is locked"). For the quick
    /// reader, which must not hold up its caller.
    pub fn never_wait_for_locks(&mut self) -> Result<()> {
        self.connection.busy_timeout(Duration::ZERO).map_err(to_djox_error)
    }

    /// Whether a statement in SQLite's `?` style changes the database (like
    /// [`SqliteConnection::statement_writes`], for SQL whose `%` are not placeholders).
    pub fn statement_as_written_writes(&mut self, sql: &str) -> Result<bool> {
        let statement = self.connection.prepare_cached(sql).map_err(to_djox_error)?;
        Ok(!statement.readonly())
    }

    /// Whether a statement in SQLite's `?` style returns rows (`SELECT`, `... RETURNING`).
    pub fn statement_as_written_returns_rows(&mut self, sql: &str) -> Result<bool> {
        let statement = self.connection.prepare_cached(sql).map_err(to_djox_error)?;
        Ok(statement.column_count() > 0)
    }

    /// Runs any one statement in SQLite's `?` style (like [`SqliteConnection::run`])
    /// without ever waiting for a lock, and, with a `time_limit`, stops it
    /// once it has run that long: for statements run right on the caller's
    /// thread, which must not hold it up.
    ///
    /// A stopped write is rolled back by SQLite. Outside a transaction that
    /// is just this statement, but inside one it is the *whole* transaction,
    /// so the caller gives no time limit for a write inside a transaction.
    pub fn run_without_waiting(
        &mut self,
        sql: &str,
        parameters: &[Value],
        time_limit: Option<Duration>,
    ) -> QuickOutcome {
        if self.connection.busy_timeout(Duration::ZERO).is_err() {
            return QuickOutcome::GaveUp;
        }
        if let Some(time_limit) = time_limit {
            let deadline = Instant::now() + time_limit;
            // SQLite calls this function every `STEPS_BETWEEN_CLOCK_CHECKS`
            // steps of the statement; answering `true` stops the statement.
            let stop_after_deadline = move || Instant::now() >= deadline;
            let _ = self
                .connection
                .progress_handler(STEPS_BETWEEN_CLOCK_CHECKS, Some(stop_after_deadline));
        }
        let rows = self.sqlite_rows_of(sql, parameters);
        if time_limit.is_some() {
            let _ = self.connection.progress_handler(0, None::<fn() -> bool>);
        }
        let _ = self.connection.busy_timeout(self.busy_timeout);
        match rows {
            Ok(rows) => QuickOutcome::Ran(Ok(StatementOutput {
                rows,
                rows_affected: self.rows_affected_by(sql),
                last_insert_id: self.connection.last_insert_rowid(),
            })),
            Err(error) => match error.sqlite_error_code() {
                // "Busy" and "locked": another connection holds the lock it
                // needs. "Interrupted": it ran out of time.
                Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked | ErrorCode::OperationInterrupted) => {
                    QuickOutcome::GaveUp
                }
                _ => QuickOutcome::Ran(Err(to_djox_error(error))),
            },
        }
    }

    /// Stops SQLite from copying the write-ahead log back into the database
    /// file (a *checkpoint*) at the end of the commit that makes the log long
    /// enough. It sets `checkpoint_wanted` instead, so that a thread made for
    /// waiting runs [`SqliteConnection::checkpoint`]: a checkpoint waits for
    /// the disk, and a commit may run on the event loop's thread (see `writer.rs`).
    pub fn checkpoint_elsewhere(&mut self, checkpoint_wanted: Arc<AtomicBool>) -> Result<()> {
        // SQLite calls this after each commit with the log's length in pages.
        // (Registering it replaces SQLite's own automatic checkpoints.)
        let note_log_length = move |_log: &mut rusqlite::hooks::Wal, pages: c_int| {
            if pages >= PAGES_BEFORE_A_CHECKPOINT {
                // An `AtomicBool` is a `bool` that threads can share without a lock.
                checkpoint_wanted.store(true, Ordering::Relaxed);
            }
            Ok(())
        };
        self.connection.wal_hook(Some(note_log_length)).map_err(to_djox_error)
    }

    /// Copies what it can of the write-ahead log back into the database file,
    /// without waiting for readers (`PASSIVE`), like SQLite's automatic checkpoints.
    pub fn checkpoint(&mut self) -> Result<()> {
        self.rows_of("PRAGMA wal_checkpoint(PASSIVE)", &[]).map(|_| ())
    }

    /// Runs a statement that changes data, written with Django's `%s` placeholders.
    pub fn execute(&mut self, sql: &str, parameters: &[Value]) -> Result<Executed> {
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
    pub fn execute_many(&mut self, sql: &str, parameter_lists: &[Vec<Value>]) -> Result<Executed> {
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

/// The values of one row, as djox values.
fn values_of(row: &rusqlite::Row<'_>, column_count: usize) -> Result<Vec<Value>> {
    let mut values = Vec::with_capacity(column_count);
    for index in 0..column_count {
        values.push(from_sqlite(row.get_ref(index).map_err(to_djox_error)?));
    }
    Ok(values)
}

/// Whether `sql` is an `INSERT`, `UPDATE`, `DELETE` or `REPLACE`: the
/// statements that change rows. Python's `sqlite3` uses the same test (the
/// first word) to decide when to report a row count.
pub fn is_data_change(sql: &str) -> bool {
    let first_word = first_word_of(sql);
    first_word == "INSERT" || first_word == "UPDATE" || first_word == "DELETE" || first_word == "REPLACE"
}

/// Whether `sql` begins or ends a transaction or a savepoint: `BEGIN`,
/// `COMMIT` (or `END`), `ROLLBACK`, `SAVEPOINT` or `RELEASE`.
pub fn is_transaction_control(sql: &str) -> bool {
    let first_word = first_word_of(sql);
    ["BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"].contains(&first_word.as_str())
}

/// The first word of `sql`, in capitals.
fn first_word_of(sql: &str) -> String {
    let first_word: String = sql
        .trim_start()
        .chars()
        .take_while(|character| character.is_ascii_alphabetic())
        .collect();
    first_word.to_ascii_uppercase()
}

/// A SQLite limit category from its number (the `SQLITE_LIMIT_...` constants
/// of SQLite's C API, also in Python's `sqlite3` module).
fn limit_from_number(category: i32) -> Option<Limit> {
    let limit = match category {
        0 => Limit::SQLITE_LIMIT_LENGTH,
        1 => Limit::SQLITE_LIMIT_SQL_LENGTH,
        2 => Limit::SQLITE_LIMIT_COLUMN,
        3 => Limit::SQLITE_LIMIT_EXPR_DEPTH,
        4 => Limit::SQLITE_LIMIT_COMPOUND_SELECT,
        5 => Limit::SQLITE_LIMIT_VDBE_OP,
        6 => Limit::SQLITE_LIMIT_FUNCTION_ARG,
        7 => Limit::SQLITE_LIMIT_ATTACHED,
        8 => Limit::SQLITE_LIMIT_LIKE_PATTERN_LENGTH,
        9 => Limit::SQLITE_LIMIT_VARIABLE_NUMBER,
        10 => Limit::SQLITE_LIMIT_TRIGGER_DEPTH,
        11 => Limit::SQLITE_LIMIT_WORKER_THREADS,
        _ => return None,
    };
    Some(limit)
}

/// A djox value as a SQLite parameter. It borrows the text or bytes (`&`)
/// instead of copying them.
pub(super) fn to_sqlite(value: &Value) -> ToSqlOutput<'_> {
    let borrowed = match value {
        Value::Null => ValueRef::Null,
        // `*number` reads the number behind the reference (numbers are cheap to copy).
        Value::Integer(number) => ValueRef::Integer(*number),
        Value::Real(number) => ValueRef::Real(*number),
        Value::Text(text) => ValueRef::Text(text.as_bytes()),
        Value::Blob(bytes) => ValueRef::Blob(bytes),
        // The typed values have no SQLite storage class of their own: they
        // become a new integer or text (`Owned`, since nothing holds it yet).
        typed => return ToSqlOutput::Owned(typed_to_sqlite(typed)),
    };
    ToSqlOutput::Borrowed(borrowed)
}

/// A typed value (see `Value`) stored the way Django's SQLite backend stores
/// it: booleans as `1` or `0`, durations as microseconds, dates, times and
/// decimals as the text Python's `isoformat()` and `str()` write.
pub(super) fn typed_to_sqlite(value: &Value) -> rusqlite::types::Value {
    use rusqlite::types::Value as Stored;
    match value {
        Value::Null => Stored::Null,
        Value::Integer(number) => Stored::Integer(*number),
        Value::Real(number) => Stored::Real(*number),
        Value::Text(text) => Stored::Text(text.clone()),
        Value::Blob(bytes) => Stored::Blob(bytes.clone()),
        Value::Boolean(boolean) => Stored::Integer(i64::from(*boolean)),
        Value::Decimal(digits) => Stored::Text(digits.clone()),
        Value::Date(date) => Stored::Text(date.format("%Y-%m-%d").to_string()),
        Value::Time(time) => Stored::Text(format!(
            "{}{}",
            time.format("%H:%M:%S"),
            microseconds(time.nanosecond())
        )),
        Value::DateTime(moment) => Stored::Text(format!(
            "{}{}",
            moment.format("%Y-%m-%d %H:%M:%S"),
            microseconds(moment.nanosecond())
        )),
        Value::DateTimeUtc(moment) => Stored::Text(format!(
            "{}{}+00:00",
            moment.format("%Y-%m-%d %H:%M:%S"),
            microseconds(moment.nanosecond())
        )),
        Value::Duration(length) => Stored::Integer(length.num_microseconds().unwrap_or(i64::MAX)),
        Value::Uuid(uuid) => Stored::Text(uuid.hyphenated().to_string()),
        // Arrays, ranges and hstores are read from Postgres only: Python's lists
        // and dicts reach the database as text, so none of these is ever sent here.
        Value::List(_) | Value::Range(_) | Value::Hstore(_) => Stored::Null,
    }
}

/// `".123456"`, or nothing for a whole second, like Python's `isoformat()`.
fn microseconds(nanoseconds: u32) -> String {
    let microseconds = nanoseconds / 1000;
    if microseconds == 0 {
        String::new()
    } else {
        format!(".{microseconds:06}")
    }
}

/// A SQLite value as a djox value (this one copies, since the row it comes
/// from is gone after the next `cursor.next()`).
pub(super) fn from_sqlite(value: ValueRef<'_>) -> Value {
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
                Error::Programming("this statement returns rows: use fetch_all() instead of execute()".to_string())
            }
            rusqlite::Error::InvalidParameterCount(_, _) | rusqlite::Error::MultipleStatement => {
                Error::Programming(message)
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
        assert!(matches!(returns_rows, Err(Error::Programming(message)) if message.contains("fetch_all")));
    }

    #[test]
    fn run_returns_rows_counts_and_ids() {
        let (_folder, mut writer) = new_writer();
        let inserted = writer
            .run(
                "INSERT INTO book (title) VALUES (?), (?)",
                &[Value::Text("A".into()), Value::Text("B".into())],
            )
            .unwrap();
        assert_eq!(inserted.rows.columns.len(), 0);
        assert_eq!(inserted.rows_affected, Some(2));
        assert_eq!(inserted.last_insert_id, 2);

        let selected = writer.run("SELECT title FROM book ORDER BY id", &[]).unwrap();
        assert_eq!(
            selected.rows.rows,
            [[Value::Text("A".into())], [Value::Text("B".into())]]
        );
        assert_eq!(selected.rows_affected, None);

        let created = writer.run("CREATE TABLE shelf (id INTEGER PRIMARY KEY)", &[]).unwrap();
        assert_eq!(
            created.rows_affected, None,
            "DDL changes no rows, whatever the last INSERT did"
        );
    }

    #[test]
    fn run_many_counts_every_run_and_refuses_queries() {
        let (_folder, mut writer) = new_writer();
        let titles = [vec![Value::Text("A".into())], vec![Value::Text("B".into())]];
        let output = writer.run_many("INSERT INTO book (title) VALUES (?)", &titles).unwrap();
        assert_eq!(output.rows_affected, Some(2));
        let refused = writer.run_many("SELECT ?", &[vec![Value::Integer(1)]]);
        assert!(matches!(refused, Err(Error::Programming(_))));
    }

    #[test]
    fn knows_when_a_transaction_is_open() {
        let (_folder, mut writer) = new_writer();
        assert!(!writer.in_transaction());
        writer.run("BEGIN IMMEDIATE", &[]).unwrap();
        assert!(writer.in_transaction());
        writer.run("ROLLBACK", &[]).unwrap();
        assert!(!writer.in_transaction());
    }

    #[test]
    fn in_memory_databases_open_without_wal() {
        let settings = SqliteSettings::new(":memory:");
        let mut connection = SqliteConnection::open(&settings, Access::Writer).unwrap();
        let journal_mode = connection.query("PRAGMA journal_mode", &[]).unwrap();
        assert_eq!(journal_mode.rows[0][0], Value::Text("memory".to_string()));
    }

    #[test]
    fn reads_limits_by_number() {
        let (_folder, writer) = new_writer();
        assert!(writer.limit(9).unwrap() > 900, "SQLITE_LIMIT_VARIABLE_NUMBER");
        let mut writer = writer;
        let old = writer.set_limit(9, 10).unwrap();
        assert!(old > 900);
        assert_eq!(writer.limit(9).unwrap(), 10);
        assert!(matches!(writer.limit(99), Err(Error::Programming(_))));
    }

    #[test]
    fn data_changes_are_found_by_their_first_word() {
        assert!(is_data_change("INSERT INTO book VALUES (1)"));
        assert!(is_data_change("  update book SET title = 'A'"));
        assert!(is_data_change("REPLACE INTO book VALUES (1)"));
        assert!(!is_data_change("SELECT 1"));
        assert!(!is_data_change("CREATE TABLE shelf (id INTEGER)"));
        assert!(!is_data_change("INSERTED"));
    }
}
