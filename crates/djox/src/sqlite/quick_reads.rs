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
//! that moment, and the SQL is remembered as slow, so its next reads go
//! straight to a reader for a while (see `statement_speeds.rs`).
//!
//! When two threads want a quick read at the same moment, the second doesn't
//! wait for the first: it sends its query to a reader.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use djox_core::{Result, Rows, Value};

use super::connection::SqliteConnection;
use super::statement_speeds::StatementSpeeds;

/// A reader connection for quick reads (see the module docs).
pub(crate) struct QuickReader {
    /// One thread at a time uses the connection (a `Mutex` is like `threading.Lock`).
    connection: Mutex<SqliteConnection>,
    /// The longest a quick read may run.
    time_limit: Duration,
    /// The reads that ran out of time lately.
    speeds: Mutex<StatementSpeeds>,
}

impl QuickReader {
    /// A quick reader on `connection`, a reader connection, prepared like the others.
    pub(crate) fn new(mut connection: SqliteConnection, time_limit: Duration) -> Result<QuickReader> {
        // Waiting for a lock would hold up the caller: fail at once, and let a reader wait instead.
        connection.never_wait_for_locks()?;
        Ok(QuickReader {
            connection: Mutex::new(connection),
            time_limit,
            speeds: Mutex::new(StatementSpeeds::default()),
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

    /// Whether `sql` writes (`INSERT ... RETURNING`), when that can be known
    /// without waiting: `false` if another thread is using the connection.
    /// Such a statement can go straight to the writer, rather than through a
    /// reader that finds out and passes it on.
    pub(crate) fn knows_statement_writes(&self, sql: &str) -> bool {
        match self.connection.try_lock() {
            Ok(mut connection) => connection.statement_writes(sql) == Ok(true),
            Err(_) => false,
        }
    }

    /// Whether `sql` ran out of time lately and this read goes to a reader.
    fn skips_slow_statement(&self, sql: &str) -> bool {
        self.speeds.lock().unwrap().skips(sql)
    }

    /// Note that `sql` ran out of time.
    fn remember_slow(&self, sql: &str) {
        self.speeds.lock().unwrap().remember_slow(sql);
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
    fn knows_which_statements_write() {
        let (_folder, _writer, reader) = quick_reader(1, Duration::from_millis(100));
        assert!(reader.knows_statement_writes("INSERT INTO book (title) VALUES (%s) RETURNING id"));
        assert!(!reader.knows_statement_writes("SELECT title FROM book"));
        let _held = reader.connection.lock().unwrap();
        assert!(
            !reader.knows_statement_writes("DELETE FROM book"),
            "busy: it can't tell"
        );
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
        assert_eq!(
            skipped_reads(&reader),
            100,
            "statement_speeds.rs skips 100 runs at first"
        );
        // Its chance came, and it ran out of time again: twice as many reads skipped.
        assert!(reader.fetch_all(slow, &[]).is_none());
        assert_eq!(skipped_reads(&reader), 2 * 100);
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
