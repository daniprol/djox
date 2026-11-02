//! [`QuickReader`]: runs small reads right on the calling thread, instead of
//! sending them to a reader thread.
//!
//! # Why
//!
//! SQLite answers a small query (one row by its id) in about 5 µs. Sending it
//! to a reader thread and getting the answer back to Python's event loop
//! takes 60 to 80 µs: the thread wakes up, and then the event loop does. So
//! for a small query the trip costs ten times the work.
//!
//! A quick read skips the trip: the calling thread runs the query itself, on
//! a connection kept for that. To keep the event loop responsive, SQLite
//! stops the query once it has run for `quick_read_limit` (1 ms by
//! default); the query then runs on a reader as usual. Nothing is lost but
//! that moment. The SQL is remembered as slow, and the next 100 reads of it
//! go straight to a reader. Then it gets another chance: a query is not
//! always slow (a cold cache, a busy machine). Each time it runs out of time
//! again, it waits twice as long for its next chance, so a query that is
//! really slow soon costs almost nothing.
//!
//! When two threads want a quick read at the same moment, the second doesn't
//! wait for the first: it sends its query to a reader.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use djox_core::{Result, Rows, Value};

use super::connection::SqliteConnection;

/// The most slow statements remembered. A program has a few dozen kinds of
/// query; past this many, the list starts over rather than growing forever.
const MAX_SLOW_STATEMENTS: usize = 1000;

/// How many reads of a statement go to a reader after it first ran out of time.
const READS_TO_SKIP_AT_FIRST: u32 = 100;

/// How many times that number doubles at most (to 100 × 2⁷ = 12,800 reads).
const MAX_DOUBLINGS: u32 = 7;

/// What the quick reader remembers of a statement that ran out of time.
struct SlowStatement {
    /// How many times it ran out of time.
    times_too_slow: u32,
    /// How many of its next reads still go to a reader, before another chance here.
    reads_to_skip: u32,
}

/// A reader connection for quick reads (see the module docs).
pub(crate) struct QuickReader {
    /// One thread at a time uses the connection (a `Mutex` is like `threading.Lock`).
    connection: Mutex<SqliteConnection>,
    /// The longest a quick read may run.
    time_limit: Duration,
    /// The reads that ran out of time, by SQL (a `HashMap` is like a `dict`).
    slow_statements: Mutex<HashMap<String, SlowStatement>>,
}

impl QuickReader {
    /// A quick reader on `connection`, a reader connection, prepared like the others.
    pub(crate) fn new(mut connection: SqliteConnection, time_limit: Duration) -> Result<QuickReader> {
        // Waiting for a lock would hold up the caller: fail at once, and let a reader wait instead.
        connection.never_wait_for_locks()?;
        Ok(QuickReader {
            connection: Mutex::new(connection),
            time_limit,
            slow_statements: Mutex::new(HashMap::new()),
        })
    }

    /// The rows of `sql`, if it is a read that finishes within the time
    /// limit; `None` if it should go to a reader instead.
    pub(crate) fn fetch_all(&self, sql: &str, parameters: &[Value]) -> Option<Rows> {
        if self.skips_slow_statement(sql) {
            return None;
        }
        // `try_lock` doesn't wait: if another thread is using the connection, give up.
        let mut connection = self.connection.try_lock().ok()?;
        let started = Instant::now();
        let rows = connection.query_within(sql, parameters, self.time_limit);
        if rows.is_none() && started.elapsed() >= self.time_limit {
            self.remember_slow(sql);
        }
        rows
    }

    /// Whether `sql` ran out of time lately and this read goes to a reader
    /// (one read fewer to skip, then).
    fn skips_slow_statement(&self, sql: &str) -> bool {
        let mut slow_statements = self.slow_statements.lock().unwrap();
        // `get_mut()` is like `dict.get()`, with a value we may change in place.
        match slow_statements.get_mut(sql) {
            Some(statement) if statement.reads_to_skip > 0 => {
                statement.reads_to_skip -= 1;
                true
            }
            _ => false,
        }
    }

    /// Note that `sql` ran out of time: skip its next reads, twice as many as last time.
    fn remember_slow(&self, sql: &str) {
        let mut slow_statements = self.slow_statements.lock().unwrap();
        if slow_statements.len() >= MAX_SLOW_STATEMENTS && !slow_statements.contains_key(sql) {
            slow_statements.clear();
        }
        // `entry().or_insert()` is like `dict.setdefault()`.
        let statement = slow_statements.entry(sql.to_string()).or_insert(SlowStatement {
            times_too_slow: 0,
            reads_to_skip: 0,
        });
        let doublings = statement.times_too_slow.min(MAX_DOUBLINGS);
        // `<<` doubles a number that many times: `100 << 2` is 400.
        statement.reads_to_skip = READS_TO_SKIP_AT_FIRST << doublings;
        statement.times_too_slow += 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sqlite::{Access, SqliteSettings};

    /// A database file with a `book` table of `count` rows, and a quick reader on it.
    fn quick_reader(count: i64, time_limit: Duration) -> (tempfile::TempDir, SqliteConnection, QuickReader) {
        let folder = tempfile::tempdir().unwrap();
        let settings = SqliteSettings::new(folder.path().join("app.sqlite3"));
        let mut writer = SqliteConnection::open(&settings, Access::Writer).unwrap();
        writer
            .execute("CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT)", &[])
            .unwrap();
        writer
            .execute(
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < %s) \
                 INSERT INTO book (id, title) SELECT i, 'Book ' || i FROM n",
                &[Value::Integer(count)],
            )
            .unwrap();
        let reader = SqliteConnection::open(&settings, Access::Reader).unwrap();
        (folder, writer, QuickReader::new(reader, time_limit).unwrap())
    }

    #[test]
    fn answers_a_small_read() {
        let (_folder, _writer, reader) = quick_reader(10, Duration::from_millis(100));
        let rows = reader.fetch_all("SELECT title FROM book WHERE id = %s", &[Value::Integer(3)]);
        assert_eq!(rows.unwrap().rows, [[Value::Text("Book 3".into())]]);
    }

    #[test]
    fn leaves_writes_to_the_writer() {
        let (_folder, _writer, reader) = quick_reader(10, Duration::from_millis(100));
        assert!(reader.fetch_all("DELETE FROM book RETURNING id", &[]).is_none());
    }

    #[test]
    fn leaves_failing_statements_to_a_reader() {
        let (_folder, _writer, reader) = quick_reader(10, Duration::from_millis(100));
        assert!(reader.fetch_all("SELECT nothing FROM book", &[]).is_none());
    }

    #[test]
    fn stops_a_slow_read_and_sends_the_next_ones_to_a_reader() {
        let (_folder, _writer, reader) = quick_reader(1, Duration::from_micros(50));
        // Counts to ten million: far longer than 50 µs.
        let slow = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 10000000) \
                    SELECT count(*) FROM n";
        let started = Instant::now();
        assert!(reader.fetch_all(slow, &[]).is_none());
        assert!(
            started.elapsed() < Duration::from_millis(50),
            "stopped soon after the limit"
        );
        assert!(reader.skips_slow_statement(slow));
        // The same connection still answers other reads afterwards.
        assert!(reader.fetch_all("SELECT count(*) FROM book", &[]).is_some());
    }

    #[test]
    fn gives_a_slow_read_another_chance_later() {
        let (_folder, _writer, reader) = quick_reader(1, Duration::from_micros(50));
        let slow = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 10000000) \
                    SELECT count(*) FROM n";
        let skipped_reads = |reader: &QuickReader| {
            let mut count = 0;
            while reader.skips_slow_statement(slow) {
                count += 1;
            }
            count
        };
        assert!(reader.fetch_all(slow, &[]).is_none());
        assert_eq!(skipped_reads(&reader), READS_TO_SKIP_AT_FIRST);
        // Its chance came, and it ran out of time again: twice as many reads skipped.
        assert!(reader.fetch_all(slow, &[]).is_none());
        assert_eq!(skipped_reads(&reader), 2 * READS_TO_SKIP_AT_FIRST);
    }

    #[test]
    fn gives_way_when_another_thread_uses_the_connection() {
        let (_folder, _writer, reader) = quick_reader(1, Duration::from_millis(100));
        let _held = reader.connection.lock().unwrap();
        assert!(reader.fetch_all("SELECT count(*) FROM book", &[]).is_none());
    }

    #[test]
    fn sees_what_the_writer_committed() {
        let (_folder, mut writer, reader) = quick_reader(1, Duration::from_millis(100));
        let count = || reader.fetch_all("SELECT count(*) FROM book", &[]).unwrap().rows;
        assert_eq!(count(), [[Value::Integer(1)]]);
        writer.execute("INSERT INTO book (title) VALUES ('New')", &[]).unwrap();
        assert_eq!(count(), [[Value::Integer(2)]]);
    }
}
