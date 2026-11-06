//! [`SharedWriter`]: the database's one writer connection, shared by the
//! writer thread, quick writes and transaction blocks.
//!
//! # Why
//!
//! SQLite runs a small `INSERT` or `UPDATE` in 10 to 40 µs. Sending it to the
//! writer thread and getting the answer back to Python's event loop takes 80
//! to 100 µs: the thread wakes up, and then the event loop does. A
//! `djox.atomic()` block with two inserts pays that trip four times (begin,
//! two inserts, commit). So, like a quick read (`quick_reads.rs`), a small
//! write runs right on the calling thread when it can, on the writer
//! connection itself. Three kinds of user share the connection:
//!
//! - the **writer thread** runs the statements sent to its queue, one by one;
//! - a **quick write** runs one statement on the calling thread;
//! - a **transaction block** (`djox.atomic()`, through a `djox.Connection`
//!   from `Database.writer_connection()`) runs `BEGIN`, its statements and
//!   `COMMIT` on it, on the calling thread when they are quick, on another
//!   Python thread when not.
//!
//! # The rules
//!
//! 1. **One thread at a time.** The connection sits behind a `Mutex` (like
//!    `threading.Lock`). Code on the calling thread never waits for it
//!    (`try_lock`): if the writer thread is busy, the statement goes to the
//!    queue instead.
//! 2. **An open transaction owns the connection.** Between `BEGIN` and
//!    `COMMIT`, only the `djox.Connection` that began it may run statements
//!    (each has a number). Everyone else waits until the transaction ends (a
//!    `Condvar`, like `threading.Condition`): the writer thread before its
//!    next job, another `djox.Connection` on the writer before its `BEGIN`.
//!    Quick writes give up instead.
//! 3. **Never hold up the calling thread.** A statement run there never waits
//!    for a lock held by another connection, and SQLite stops it once it has
//!    run for `quick_write_limit` (1 ms by default); it then runs the usual
//!    way. Inside a transaction a write can't be stopped (SQLite would roll
//!    back the whole transaction), so a write runs on the calling thread
//!    there only if the same SQL finished within the limit the last time it
//!    ran (see `statement_speeds.rs`).
//! 4. **Checkpoints run where waiting is fine.** Copying the write-ahead log
//!    back into the database file waits for the disk, so the commit that
//!    makes the log long only notes it, and the next statement that runs the
//!    usual way (on the writer thread, or another thread) does the checkpoint.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use djox_core::{Error, Result, Value};

use super::connection::{QuickOutcome, SqliteConnection, is_data_change, is_transaction_control};
use super::settings::SqliteSettings;
use super::statement_speeds::StatementSpeeds;

/// The writer connection and what its users need to share it (see the module docs).
pub struct SharedWriter {
    writing: Mutex<Writing>,
    /// Woken when a transaction ends, for whoever waits to use the connection.
    transaction_ended: Condvar,
    /// How long to wait for another user's transaction to end, before
    /// failing with "database is locked" (the `timeout` setting).
    busy_timeout: Duration,
    /// The longest a statement may run on the calling thread; zero: never.
    quick_write_limit: Duration,
    /// Which statements were quick lately, and which ran out of time.
    speeds: Mutex<StatementSpeeds>,
    /// Set when the write-ahead log is long enough for a checkpoint (rule 4).
    checkpoint_wanted: Arc<AtomicBool>,
    /// The number the next `djox.Connection` on this writer gets.
    next_user_number: AtomicU64,
}

/// What sits behind the writer's lock: the connection, and who owns its open transaction.
pub(crate) struct Writing {
    pub(crate) connection: SqliteConnection,
    /// The number of the `djox.Connection` whose transaction is open (rule
    /// 2), or `None`. (Plain writes never leave a transaction open.)
    transaction_owner: Option<u64>,
}

impl SharedWriter {
    /// Shares `connection`, the database's writer connection, already prepared.
    pub(crate) fn new(mut connection: SqliteConnection, settings: &SqliteSettings) -> Result<SharedWriter> {
        let checkpoint_wanted = Arc::new(AtomicBool::new(false));
        connection.checkpoint_elsewhere(Arc::clone(&checkpoint_wanted))?;
        Ok(SharedWriter {
            writing: Mutex::new(Writing {
                connection,
                transaction_owner: None,
            }),
            transaction_ended: Condvar::new(),
            busy_timeout: settings.busy_timeout,
            quick_write_limit: settings.quick_write_limit,
            speeds: Mutex::new(StatementSpeeds::default()),
            checkpoint_wanted,
            next_user_number: AtomicU64::new(1),
        })
    }

    /// A number of its own, for a new `djox.Connection` on this writer: it
    /// tells whose transaction is open.
    pub fn new_user_number(&self) -> u64 {
        self.next_user_number.fetch_add(1, Ordering::Relaxed)
    }

    /// For the writer thread: the connection, once no transaction is open on
    /// it (rule 2), or "database is locked" after waiting `busy_timeout`.
    ///
    /// A `MutexGuard` is the lock while it is held: the connection is
    /// unlocked when the guard is dropped (like leaving a `with lock:` block).
    pub(crate) fn connection_for_a_queued_job(&self) -> Result<MutexGuard<'_, Writing>> {
        let mut writing = self.wait_for_the_transaction_of_others(None)?;
        self.checkpoint_if_wanted(&mut writing.connection);
        Ok(writing)
    }

    /// For the writer thread, after a queued job: a queued statement commits
    /// on its own, so one that left a transaction open (a plain `BEGIN` sent
    /// to `execute()`) is rolled back. It would keep the writer waiting forever.
    pub(crate) fn finish_queued_job(&self, writing: &mut Writing) {
        if writing.connection.in_transaction() {
            let _ = writing.connection.run("ROLLBACK", &[]);
        }
    }

    /// Runs `action` with the connection for the `djox.Connection` numbered
    /// `user`, waiting if another thread uses it, or another user's
    /// transaction is open. A `djox.Connection` blocks its caller, like any
    /// sync connection (with the GIL released).
    pub fn with_connection<T>(&self, user: u64, action: impl FnOnce(&mut SqliteConnection) -> Result<T>) -> Result<T> {
        let mut writing = self.wait_for_the_transaction_of_others(Some(user))?;
        if !writing.connection.in_transaction() {
            self.checkpoint_if_wanted(&mut writing.connection);
        }
        let result = action(&mut writing.connection);
        self.note_the_transaction_owner(&mut writing, user);
        result
    }

    /// For the `djox.Connection` numbered `user`: runs any one statement in
    /// SQLite's `?` style right here, if it can be done without waiting (see
    /// the module docs), or gives up; then the caller runs it the usual way.
    pub fn run_quickly(&self, user: u64, sql: &str, parameters: &[Value]) -> QuickOutcome {
        // `try_lock` doesn't wait: if another thread is using the connection, give up.
        let Ok(mut writing) = self.writing.try_lock() else {
            return QuickOutcome::GaveUp;
        };
        if writing.transaction_owner.is_some_and(|owner| owner != user) {
            return QuickOutcome::GaveUp; // another user's transaction: wait for it the usual way
        }
        let outcome = self.run_here_if_quick(&mut writing.connection, sql, parameters);
        self.note_the_transaction_owner(&mut writing, user);
        outcome
    }

    /// A plain write (`Database.execute()` and `fetch_all()`), right here if
    /// it can be done without waiting, or `GaveUp`: then it goes to the
    /// writer thread. Never inside a transaction: a plain write commits on
    /// its own. `rows_allowed` is `false` for `execute()`, which refuses a
    /// statement that returns rows (the writer thread reports the mistake).
    pub fn write_quickly(&self, sql: &str, parameters: &[Value], rows_allowed: bool) -> QuickOutcome {
        let Ok(mut writing) = self.writing.try_lock() else {
            return QuickOutcome::GaveUp;
        };
        let connection = &mut writing.connection;
        if connection.in_transaction() || !is_data_change(sql) {
            return QuickOutcome::GaveUp;
        }
        if !rows_allowed && connection.statement_as_written_returns_rows(sql) != Ok(false) {
            return QuickOutcome::GaveUp;
        }
        self.run_here_if_quick(connection, sql, parameters)
    }

    /// Rolls back the transaction of the `djox.Connection` numbered `user`,
    /// if it is open, without making the caller wait: if another thread is
    /// using the connection, a new thread waits for it and rolls back. For a
    /// `djox.Connection` that closes (or is dropped) with its transaction
    /// still open, which would keep the writer waiting.
    ///
    /// `self: &Arc<Self>`: called on a shared reference, which the new thread can keep.
    pub fn roll_back_without_waiting(self: &Arc<Self>, user: u64) {
        if let Ok(mut writing) = self.writing.try_lock() {
            self.roll_back_the_transaction_of(&mut writing, user);
            return;
        }
        let writer = Arc::clone(self);
        let _ = std::thread::Builder::new()
            .name("djox-sqlite-rollback".to_string())
            .spawn(move || {
                let mut writing = writer.writing.lock().unwrap();
                writer.roll_back_the_transaction_of(&mut writing, user);
            });
    }

    /// Notes how long `sql` took, so the next run of it knows whether it is quick (rule 3).
    pub fn remember_how_long(&self, sql: &str, took: Duration) {
        let mut speeds = self.speeds.lock().unwrap();
        if took < self.quick_write_limit {
            speeds.remember_quick(sql);
        } else {
            speeds.remember_slow(sql);
        }
    }

    /// The writer, locked, once no transaction is open but `user`'s own (rule
    /// 2), or "database is locked" after waiting `busy_timeout`.
    fn wait_for_the_transaction_of_others(&self, user: Option<u64>) -> Result<MutexGuard<'_, Writing>> {
        let deadline = Instant::now() + self.busy_timeout;
        let mut writing = self.writing.lock().unwrap();
        while writing.transaction_owner.is_some() && writing.transaction_owner != user {
            let now = Instant::now();
            if now >= deadline {
                return Err(Error::Operational(format!(
                    "database is locked: a transaction on djox's writer connection lasted more than {} seconds \
                     (the 'timeout' option)",
                    self.busy_timeout.as_secs_f64()
                )));
            }
            // `wait_timeout` unlocks the writer while it sleeps, and locks it
            // again when woken, like `threading.Condition.wait(timeout)`.
            writing = self.transaction_ended.wait_timeout(writing, deadline - now).unwrap().0;
        }
        Ok(writing)
    }

    /// Runs one statement right here, with the rules of the module docs, or gives up.
    fn run_here_if_quick(&self, connection: &mut SqliteConnection, sql: &str, parameters: &[Value]) -> QuickOutcome {
        if self.quick_write_limit.is_zero() || self.checkpoint_wanted.load(Ordering::Relaxed) {
            return QuickOutcome::GaveUp;
        }
        let in_transaction = connection.in_transaction();
        let Ok(writes) = connection.statement_as_written_writes(sql) else {
            return QuickOutcome::GaveUp; // it doesn't compile: the usual way reports the error
        };
        let time_limit = {
            let mut speeds = self.speeds.lock().unwrap();
            if is_transaction_control(sql) {
                // `BEGIN`, `COMMIT`, `ROLLBACK`: there is nothing to stop halfway.
                None
            } else if writes && in_transaction {
                // Rule 3: no time limit for a write inside a transaction, so only a known quick one runs here.
                if !speeds.is_known_quick(sql) {
                    return QuickOutcome::GaveUp;
                }
                None
            } else {
                // Outside a transaction, only `INSERT`, `UPDATE`, `DELETE` and `REPLACE`
                // run here: a `CREATE TABLE` is for the writer thread.
                if (writes && !is_data_change(sql)) || speeds.skips(sql) {
                    return QuickOutcome::GaveUp;
                }
                Some(self.quick_write_limit)
            }
        };
        let started = Instant::now();
        let outcome = connection.run_without_waiting(sql, parameters, time_limit);
        self.remember_how_long(sql, started.elapsed());
        outcome
    }

    /// After `user` ran a statement: the open transaction (if any) is theirs,
    /// and if none is open any more, whoever waits for one to end may go on.
    fn note_the_transaction_owner(&self, writing: &mut Writing, user: u64) {
        if writing.connection.in_transaction() {
            writing.transaction_owner = Some(user);
        } else {
            writing.transaction_owner = None;
            self.transaction_ended.notify_all();
        }
    }

    fn roll_back_the_transaction_of(&self, writing: &mut Writing, user: u64) {
        if writing.transaction_owner == Some(user) {
            let _ = writing.connection.run("ROLLBACK", &[]);
            self.note_the_transaction_owner(writing, user);
        }
    }

    /// Runs a checkpoint if a commit asked for one (rule 4). Only call this
    /// where waiting is fine, with no transaction open.
    fn checkpoint_if_wanted(&self, connection: &mut SqliteConnection) {
        // `swap` sets the flag to `false` and returns what it was, in one step.
        if self.checkpoint_wanted.swap(false, Ordering::Relaxed) {
            // A failed checkpoint (a reader holds an old snapshot) is simply tried again later.
            let _ = connection.checkpoint();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sqlite::{Access, StatementOutput};

    /// A shared writer on a new database with a `book` table.
    fn shared_writer() -> (tempfile::TempDir, SqliteSettings, SharedWriter) {
        let folder = tempfile::tempdir().unwrap();
        let mut settings = SqliteSettings::new(folder.path().join("app.sqlite3"));
        settings.busy_timeout = Duration::from_millis(200);
        let mut connection = SqliteConnection::open(&settings, Access::Writer).unwrap();
        connection
            .execute("CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT UNIQUE)", &[])
            .unwrap();
        let writer = SharedWriter::new(connection, &settings).unwrap();
        (folder, settings, writer)
    }

    fn ran(outcome: QuickOutcome) -> Result<StatementOutput> {
        match outcome {
            QuickOutcome::Ran(result) => result,
            QuickOutcome::GaveUp => panic!("expected the statement to run"),
        }
    }

    /// The number of the `djox.Connection` the tests play.
    const USER: u64 = 1;

    fn count_books(writer: &SharedWriter) -> Value {
        let output = writer.with_connection(USER, |connection| connection.run("SELECT count(*) FROM book", &[]));
        output.unwrap().rows.rows[0][0].clone()
    }

    #[test]
    fn a_small_write_runs_quickly() {
        let (_folder, _settings, writer) = shared_writer();
        let insert = "INSERT INTO book (title) VALUES (?)";
        let output = ran(writer.write_quickly(insert, &[Value::Text("Dune".into())], false));
        assert_eq!(output.unwrap().rows_affected, Some(1));
        assert_eq!(count_books(&writer), Value::Integer(1));
    }

    #[test]
    fn a_failing_write_reports_its_error() {
        let (_folder, _settings, writer) = shared_writer();
        let insert = "INSERT INTO book (title) VALUES ('Dune')";
        ran(writer.write_quickly(insert, &[], false)).unwrap();
        assert!(matches!(
            ran(writer.write_quickly(insert, &[], false)),
            Err(Error::Integrity(_))
        ));
    }

    #[test]
    fn schema_changes_are_left_to_the_writer_thread() {
        let (_folder, _settings, writer) = shared_writer();
        let outcome = writer.write_quickly("CREATE TABLE author (id INTEGER PRIMARY KEY)", &[], false);
        assert!(matches!(outcome, QuickOutcome::GaveUp));
    }

    #[test]
    fn gives_up_when_another_connection_holds_the_lock() {
        let (_folder, settings, writer) = shared_writer();
        let mut other = SqliteConnection::open(&settings, Access::Writer).unwrap();
        other.run("BEGIN IMMEDIATE", &[]).unwrap();
        let started = Instant::now();
        let outcome = writer.write_quickly("INSERT INTO book (title) VALUES ('Dune')", &[], false);
        assert!(matches!(outcome, QuickOutcome::GaveUp));
        assert!(
            started.elapsed() < Duration::from_millis(100),
            "it didn't wait for the lock"
        );
        other.run("ROLLBACK", &[]).unwrap();
    }

    #[test]
    fn inside_a_transaction_only_known_quick_writes_run_quickly() {
        let (_folder, _settings, writer) = shared_writer();
        let insert = "INSERT INTO book (title) VALUES (?)";
        ran(writer.run_quickly(USER, "BEGIN IMMEDIATE", &[])).unwrap();
        let unknown = writer.run_quickly(USER, insert, &[Value::Text("Dune".into())]);
        assert!(
            matches!(unknown, QuickOutcome::GaveUp),
            "never ran before: it might be slow"
        );
        writer.remember_how_long(insert, Duration::from_micros(10));
        ran(writer.run_quickly(USER, insert, &[Value::Text("Dune".into())])).unwrap();
        ran(writer.run_quickly(USER, "COMMIT", &[])).unwrap();
        assert_eq!(count_books(&writer), Value::Integer(1));
    }

    #[test]
    fn the_writer_thread_waits_for_an_open_transaction() {
        let (_folder, _settings, writer) = shared_writer();
        let writer = Arc::new(writer);
        ran(writer.run_quickly(USER, "BEGIN IMMEDIATE", &[])).unwrap();
        let queued = {
            let writer = Arc::clone(&writer);
            std::thread::spawn(move || {
                let started = Instant::now();
                let mut writing = writer.connection_for_a_queued_job().unwrap();
                writing
                    .connection
                    .execute("INSERT INTO book (title) VALUES ('Queued')", &[])
                    .unwrap();
                started.elapsed()
            })
        };
        std::thread::sleep(Duration::from_millis(50));
        writer
            .with_connection(USER, |connection| {
                connection.run("INSERT INTO book (title) VALUES ('Block')", &[])
            })
            .unwrap();
        writer
            .with_connection(USER, |connection| connection.run("COMMIT", &[]))
            .unwrap();
        assert!(
            queued.join().unwrap() >= Duration::from_millis(50),
            "it waited for the commit"
        );
        assert_eq!(count_books(&writer), Value::Integer(2));
    }

    #[test]
    fn the_writer_thread_gives_up_on_a_transaction_that_lasts_too_long() {
        let (_folder, _settings, writer) = shared_writer();
        ran(writer.run_quickly(USER, "BEGIN IMMEDIATE", &[])).unwrap();
        let waited = writer.connection_for_a_queued_job();
        assert!(matches!(waited, Err(Error::Operational(message)) if message.contains("database is locked")));
    }

    #[test]
    fn a_queued_job_cannot_leave_a_transaction_open() {
        let (_folder, _settings, writer) = shared_writer();
        let mut writing = writer.connection_for_a_queued_job().unwrap();
        writing.connection.run("BEGIN", &[]).unwrap();
        writer.finish_queued_job(&mut writing);
        assert!(!writing.connection.in_transaction());
    }

    #[test]
    fn plain_writes_never_run_inside_a_transaction() {
        let (_folder, _settings, writer) = shared_writer();
        ran(writer.run_quickly(USER, "BEGIN IMMEDIATE", &[])).unwrap();
        let outcome = writer.write_quickly("INSERT INTO book (title) VALUES ('Dune')", &[], false);
        assert!(matches!(outcome, QuickOutcome::GaveUp));
        ran(writer.run_quickly(USER, "ROLLBACK", &[])).unwrap();
    }

    #[test]
    fn another_users_begin_waits_for_the_open_transaction() {
        let (_folder, _settings, writer) = shared_writer();
        let writer = Arc::new(writer);
        let other_user = writer.new_user_number() + 1;
        ran(writer.run_quickly(USER, "BEGIN IMMEDIATE", &[])).unwrap();
        assert!(matches!(
            writer.run_quickly(other_user, "BEGIN IMMEDIATE", &[]),
            QuickOutcome::GaveUp
        ));
        let other = {
            let writer = Arc::clone(&writer);
            std::thread::spawn(move || {
                writer.with_connection(other_user, |connection| connection.run("BEGIN IMMEDIATE", &[]))?;
                writer.with_connection(other_user, |connection| connection.run("COMMIT", &[]))
            })
        };
        std::thread::sleep(Duration::from_millis(50));
        writer
            .with_connection(USER, |connection| connection.run("COMMIT", &[]))
            .unwrap();
        assert!(
            other.join().unwrap().is_ok(),
            "it began once the first transaction ended"
        );
    }

    #[test]
    fn rolling_back_only_touches_ones_own_transaction() {
        let (_folder, _settings, writer) = shared_writer();
        let writer = Arc::new(writer);
        ran(writer.run_quickly(USER, "BEGIN IMMEDIATE", &[])).unwrap();
        writer.roll_back_without_waiting(USER + 1);
        assert!(matches!(
            writer.write_quickly("INSERT INTO book (title) VALUES ('x')", &[], false),
            QuickOutcome::GaveUp
        ));
        writer.roll_back_without_waiting(USER);
        ran(writer.write_quickly("INSERT INTO book (title) VALUES ('x')", &[], false)).unwrap();
    }
}
