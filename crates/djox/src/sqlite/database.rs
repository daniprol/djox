//! [`SqliteDatabase`]: opens the connections, starts the workers and sends
//! each statement to the right queue.

use std::sync::{Arc, Mutex};

use crossbeam_channel::Sender;
use djox_core::{ChunkRequests, Database, Error, Executed, ReplySender, Result, Rows, Value};

use super::connection::{Access, QuickOutcome, SqliteConnection};
use super::placeholders::to_question_marks;
use super::quick_reads::QuickReader;
use super::readers::ReaderQueue;
use super::settings::SqliteSettings;
use super::worker::{Job, start_reader, start_writer};
use super::writer::SharedWriter;

/// A SQLite database with one writer connection and a few reader
/// connections, each on its own thread (see the module docs for why).
///
/// - `execute` and `execute_many` go to the writer, one at a time, in the
///   order they were sent, so they never fail with "database is locked".
/// - `execute_quickly` runs a small write right on the calling thread, on
///   the writer connection itself (see `writer.rs`).
/// - `fetch_all` goes to whichever reader is free. A statement that writes
///   (like `INSERT ... RETURNING`) is passed on to the writer.
/// - `fetch_all_quickly` runs a small read right on the calling thread, on a
///   connection of its own (see `quick_reads.rs`).
/// - `stream_rows` opens a reader connection of its own, on a thread of its
///   own, for as long as the stream lasts.
pub struct SqliteDatabase {
    /// The two queues, or `None` once the database is closed.
    ///
    /// `Option<T>` is Rust's "`T` or `None`". The `Mutex` (like
    /// `threading.Lock`) lets many threads read and change it safely: each
    /// one locks it just long enough to copy a queue sender.
    queues: Mutex<Option<Queues>>,
    settings: SqliteSettings,
    /// The connection for quick reads, or `None` if they are off.
    quick_reader: Option<QuickReader>,
    /// The writer connection, shared by the writer thread, quick writes and
    /// transaction blocks (see `writer.rs`).
    writer: Arc<SharedWriter>,
    /// What `open_with` was given to prepare each new connection, kept for
    /// the connections streams open later.
    prepare: Arc<Prepare>,
}

/// A function that prepares a new connection (see [`SqliteDatabase::open_with`]).
/// `dyn Fn(...)` is "any function with this signature", like a `Callable` type hint.
type Prepare = dyn Fn(SqliteConnection) -> Result<SqliteConnection> + Send + Sync;

/// The writer's queue, and the queue all readers share.
struct Queues {
    writer: Sender<Job>,
    /// `None` when there are no readers: the writer runs the reads too.
    readers: Option<Arc<ReaderQueue>>,
}

impl SqliteDatabase {
    /// Opens the writer connection and the reader connections, and starts a
    /// worker thread for each.
    ///
    /// The writer opens first: it creates the file and turns on WAL mode
    /// before the readers open it.
    pub fn open(settings: SqliteSettings) -> Result<SqliteDatabase> {
        // `Ok` itself is the `prepare` that changes nothing: it hands each connection straight back.
        SqliteDatabase::open_with(settings, Ok)
    }

    /// Like [`SqliteDatabase::open`], and calls `prepare` once with each new
    /// connection before it starts working: the place to register SQL
    /// functions or run `PRAGMA`s, like Django does on every connection.
    ///
    /// `prepare` takes the connection and gives it back. Taking it (instead of
    /// borrowing it) lets the Python extension wrap it in a Python object for
    /// the duration of the call. It is kept for the connections of streams,
    /// so it must be usable from any thread (`Send + Sync`) and own what it
    /// uses (`'static`).
    pub fn open_with(
        settings: SqliteSettings,
        prepare: impl Fn(SqliteConnection) -> Result<SqliteConnection> + Send + Sync + 'static,
    ) -> Result<SqliteDatabase> {
        settings.check()?;
        let writer_connection = prepare(SqliteConnection::open(&settings, Access::Writer)?)?;
        let mut reader_connections = Vec::new();
        for _ in 0..settings.reader_count {
            reader_connections.push(prepare(SqliteConnection::open(&settings, Access::Reader)?)?);
        }
        let quick_reader = if settings.quick_read_limit.is_zero() {
            None
        } else {
            let connection = prepare(SqliteConnection::open(&settings, Access::Reader)?)?;
            Some(QuickReader::new(connection, settings.quick_read_limit)?)
        };

        // An unbounded channel is like Python's `queue.SimpleQueue`: sending never waits.
        let (writer_queue, writer_jobs) = crossbeam_channel::unbounded();
        let writer = Arc::new(SharedWriter::new(writer_connection, &settings)?);
        start_writer(Arc::clone(&writer), writer_jobs).map_err(could_not_start_thread)?;

        let readers_queue = if settings.reader_count == 0 {
            None
        } else {
            let readers_queue = Arc::new(ReaderQueue::new());
            // `enumerate()` is Python's `enumerate()`.
            for (number, connection) in reader_connections.into_iter().enumerate() {
                let name = format!("djox-sqlite-reader-{number}");
                // `Arc::clone` hands out one more reference to the same queue.
                start_reader(name, connection, Arc::clone(&readers_queue), writer_queue.clone())
                    .map_err(could_not_start_thread)?;
            }
            Some(readers_queue)
        };

        let queues = Queues {
            writer: writer_queue,
            readers: readers_queue,
        };
        Ok(SqliteDatabase {
            queues: Mutex::new(Some(queues)),
            settings,
            quick_reader,
            writer,
            prepare: Arc::new(prepare),
        })
    }

    /// The settings the database was opened with.
    pub fn settings(&self) -> &SqliteSettings {
        &self.settings
    }

    /// The writer connection, for a transaction block that runs its
    /// statements on it (`Database.writer_connection()` in Python).
    pub fn writer(&self) -> Arc<SharedWriter> {
        Arc::clone(&self.writer)
    }

    /// A quick write (see `writer.rs`): the output of `sql`, written with
    /// Django's `%s` placeholders, if it ran right here, or `None`.
    ///
    /// A write that failed gives `None` too: it changed nothing (outside a
    /// transaction, SQLite rolls back a failed statement), and the writer
    /// thread runs it again and reports the error.
    fn write_quickly(&self, sql: &str, parameters: &[Value], rows_allowed: bool) -> Option<super::StatementOutput> {
        if self.is_closed() {
            return None; // the usual way answers `Error::Closed`
        }
        match self
            .writer
            .write_quickly(&to_question_marks(sql), parameters, rows_allowed)
        {
            QuickOutcome::Ran(Ok(output)) => Some(output),
            QuickOutcome::Ran(Err(_)) | QuickOutcome::GaveUp => None,
        }
    }

    /// Puts a job in the writer's queue.
    fn send_to_writer(&self, job: Job) {
        // `.lock().unwrap()` takes the lock; `unwrap` would only fail if another
        // thread crashed while holding it. `.map(...)` copies the sender if the
        // queues are there, and gives `None` if the database is closed.
        let queue = self.queues.lock().unwrap().as_ref().map(|queues| queues.writer.clone());
        send(queue, job);
    }

    /// Puts a job in the readers' queue (the writer's, if there are no readers).
    fn send_to_readers(&self, job: Job) {
        let queues = self.queues.lock().unwrap();
        match queues.as_ref() {
            // Closed: dropping the job answers its caller with `Error::Closed`.
            None => {}
            Some(Queues {
                readers: Some(readers), ..
            }) => readers.send(job),
            Some(Queues { writer, readers: None }) => {
                let _ = writer.send(job);
            }
        }
    }
}

/// Puts `job` in `queue`. If the database is closed, the job is dropped,
/// which answers its caller with `Error::Closed`.
fn send(queue: Option<Sender<Job>>, job: Job) {
    if let Some(queue) = queue {
        // `send` only fails if the workers are gone; then the job is dropped too.
        let _ = queue.send(job);
    }
}

fn could_not_start_thread(error: std::io::Error) -> Error {
    Error::Operational(format!("could not start a database worker thread: {error}"))
}

/// `impl Database for SqliteDatabase` is how a type says "I implement this
/// trait", like subclassing an abstract base class and filling in its methods.
impl Database for SqliteDatabase {
    fn backend_name(&self) -> &str {
        "sqlite"
    }

    fn fetch_all(&self, sql: String, parameters: Vec<Value>, reply: ReplySender<Rows>) {
        // A statement that writes and returns rows (`INSERT ... RETURNING`)
        // would go to a reader, which would pass it on to the writer: one
        // thread hop too many. The quick reader's connection can tell first.
        // (`is_some_and` is "there is one, and the check is true".)
        let writes = self
            .quick_reader
            .as_ref()
            .is_some_and(|quick_reader| quick_reader.knows_statement_writes(&sql));
        if writes {
            self.send_to_writer(Job::FetchAll { sql, parameters, reply });
        } else {
            self.send_to_readers(Job::FetchAll { sql, parameters, reply });
        }
    }

    fn fetch_all_quickly(&self, sql: &str, parameters: &[Value]) -> Option<Rows> {
        if self.is_closed() {
            return None; // `fetch_all` answers `Error::Closed`
        }
        // `?` on an `Option` returns `None` right away when there is no quick reader.
        let quick_reader = self.quick_reader.as_ref()?;
        if quick_reader.knows_statement_writes(sql) {
            // `INSERT ... RETURNING`: a quick write.
            return self.write_quickly(sql, parameters, true).map(|output| output.rows);
        }
        quick_reader.fetch_all(sql, parameters)
    }

    fn stream_rows(&self, sql: String, parameters: Vec<Value>, chunk_size: usize, requests: ChunkRequests) {
        if self.is_closed() {
            return; // dropping `requests` answers the first chunk with `Error::Closed`
        }
        let settings = self.settings.clone();
        let prepare = Arc::clone(&self.prepare);
        // A thread and a connection of its own (see the trait's docs). If the
        // thread can't start, `requests` is dropped with it: `Error::Closed`.
        let _ = std::thread::Builder::new()
            .name("djox-sqlite-stream".to_string())
            .spawn(move || {
                let opened =
                    SqliteConnection::open(&settings, Access::Reader).and_then(|connection| prepare(connection));
                match opened {
                    Ok(mut connection) => connection.stream(&sql, &parameters, chunk_size.max(1), &requests),
                    Err(error) => {
                        if let Some(reply) = requests.next() {
                            reply.send(Err(error));
                        }
                    }
                }
            });
    }

    fn execute(&self, sql: String, parameters: Vec<Value>, reply: ReplySender<Executed>) {
        self.send_to_writer(Job::Execute { sql, parameters, reply });
    }

    fn execute_quickly(&self, sql: &str, parameters: &[Value]) -> Option<Executed> {
        let output = self.write_quickly(sql, parameters, false)?;
        Some(Executed {
            rows_affected: output.rows_affected.unwrap_or(0),
            last_insert_id: output.last_insert_id,
        })
    }

    fn execute_many(&self, sql: String, parameter_lists: Vec<Vec<Value>>, reply: ReplySender<Executed>) {
        self.send_to_writer(Job::ExecuteMany {
            sql,
            parameter_lists,
            reply,
        });
    }

    fn close(&self) {
        // Dropping the writer's queue sender and closing the readers' queue
        // stops new statements. Each worker finishes what is already queued,
        // then stops and closes its connection.
        let queues = self.queues.lock().unwrap().take();
        if let Some(Queues {
            readers: Some(readers), ..
        }) = queues
        {
            readers.close();
        }
    }

    fn is_closed(&self) -> bool {
        self.queues.lock().unwrap().is_none()
    }
}

/// Dropping the database (like `__del__`) closes it, so its threads stop.
impl Drop for SqliteDatabase {
    fn drop(&mut self) {
        self.close();
    }
}

#[cfg(test)]
mod tests {
    use djox_core::Reply;

    use super::*;

    fn open(reader_count: usize) -> (tempfile::TempDir, SqliteDatabase) {
        let folder = tempfile::tempdir().unwrap();
        let mut settings = SqliteSettings::new(folder.path().join("app.sqlite3"));
        settings.reader_count = reader_count;
        let database = SqliteDatabase::open(settings).unwrap();
        execute(
            &database,
            "CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT)",
            vec![],
        )
        .unwrap();
        (folder, database)
    }

    /// Sends a query and blocks until its answer arrives.
    fn fetch_all(database: &SqliteDatabase, sql: &str, parameters: Vec<Value>) -> Result<Rows> {
        let (sender, reply) = Reply::new();
        database.fetch_all(sql.to_string(), parameters, sender);
        reply.wait_blocking()
    }

    /// Sends a write and blocks until its answer arrives.
    fn execute(database: &SqliteDatabase, sql: &str, parameters: Vec<Value>) -> Result<Executed> {
        let (sender, reply) = Reply::new();
        database.execute(sql.to_string(), parameters, sender);
        reply.wait_blocking()
    }

    fn count_books(database: &SqliteDatabase) -> Value {
        fetch_all(database, "SELECT count(*) FROM book", vec![]).unwrap().rows[0][0].clone()
    }

    #[test]
    fn readers_see_what_the_writer_committed() {
        let (_folder, database) = open(2);
        execute(
            &database,
            "INSERT INTO book (title) VALUES (%s)",
            vec![Value::Text("Dune".into())],
        )
        .unwrap();
        assert_eq!(count_books(&database), Value::Integer(1));
    }

    #[test]
    fn fetch_all_passes_writes_to_the_writer() {
        let (_folder, database) = open(2);
        let rows = fetch_all(
            &database,
            "INSERT INTO book (title) VALUES ('Dune') RETURNING id, title",
            vec![],
        )
        .unwrap();
        assert_eq!(rows.rows, [[Value::Integer(1), Value::Text("Dune".into())]]);
    }

    #[test]
    fn many_concurrent_writes_and_reads_never_hit_a_lock() {
        let (_folder, database) = open(4);
        // Send 200 writes and 200 reads at once, then wait for all of them.
        let mut writes = Vec::new();
        let mut reads = Vec::new();
        for number in 0..200 {
            let (write_sender, write) = Reply::new();
            let title = Value::Text(format!("Book {number}"));
            database.execute("INSERT INTO book (title) VALUES (%s)".into(), vec![title], write_sender);
            writes.push(write);

            let (read_sender, read) = Reply::new();
            database.fetch_all("SELECT count(*) FROM book".into(), vec![], read_sender);
            reads.push(read);
        }
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(async {
            for write in writes {
                assert_eq!(write.wait().await.unwrap().rows_affected, 1);
            }
            for read in reads {
                read.wait().await.unwrap();
            }
        });
        assert_eq!(count_books(&database), Value::Integer(200));
    }

    #[test]
    fn quick_reads_answer_small_reads_and_leave_the_rest() {
        let (_folder, database) = open(1);
        let rows = database.fetch_all_quickly("SELECT count(*) FROM book", &[]).unwrap();
        assert_eq!(rows.rows, [[Value::Integer(0)]]);
        // A write that returns rows is a quick write.
        let inserted = database
            .fetch_all_quickly("INSERT INTO book (title) VALUES ('Dune') RETURNING id", &[])
            .unwrap();
        assert_eq!(inserted.rows, [[Value::Integer(1)]]);
        let slow = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 100000000) \
                    SELECT count(*) FROM n";
        assert!(
            database.fetch_all_quickly(slow, &[]).is_none(),
            "too slow: for a reader"
        );
        database.close();
        assert!(database.fetch_all_quickly("SELECT 1", &[]).is_none(), "closed");
    }

    #[test]
    fn quick_reads_can_be_turned_off() {
        let folder = tempfile::tempdir().unwrap();
        let mut settings = SqliteSettings::new(folder.path().join("app.sqlite3"));
        settings.quick_read_limit = std::time::Duration::ZERO;
        let database = SqliteDatabase::open(settings).unwrap();
        assert!(database.fetch_all_quickly("SELECT 1", &[]).is_none());
    }

    #[test]
    fn works_with_no_readers() {
        let (_folder, database) = open(0);
        assert_eq!(count_books(&database), Value::Integer(0));
    }

    #[test]
    fn closing_finishes_sent_work_and_refuses_new_work() {
        let (_folder, database) = open(1);
        let (sender, sent) = Reply::new();
        database.execute("INSERT INTO book (title) VALUES ('Dune')".into(), vec![], sender);
        database.close();
        assert!(database.is_closed());
        assert_eq!(sent.wait_blocking().unwrap().rows_affected, 1);
        assert_eq!(fetch_all(&database, "SELECT 1", vec![]).unwrap_err(), Error::Closed);
        database.close(); // closing twice does nothing
    }

    #[test]
    fn prepare_runs_on_every_connection() {
        let folder = tempfile::tempdir().unwrap();
        let mut settings = SqliteSettings::new(folder.path().join("app.sqlite3"));
        settings.reader_count = 2;
        // `Arc<Mutex<...>>`: a counter shared with the closure (which the
        // database keeps), behind a lock (like `threading.Lock`).
        let prepared = Arc::new(Mutex::new(0));
        let counter = Arc::clone(&prepared);
        let database = SqliteDatabase::open_with(settings, move |mut connection| {
            *counter.lock().unwrap() += 1;
            connection.create_scalar_function("answer", 0, true, |_| Ok(Value::Integer(42)))?;
            Ok(connection)
        })
        .unwrap();

        assert_eq!(
            *prepared.lock().unwrap(),
            4,
            "the writer, two readers and the quick reader"
        );
        let rows = fetch_all(&database, "SELECT answer()", vec![]).unwrap();
        assert_eq!(rows.rows, [[Value::Integer(42)]]);
    }

    /// Asks a stream for its next chunk and blocks until it arrives.
    fn next_chunk(stream: &djox_core::RowStream) -> Result<Rows> {
        let (sender, reply) = Reply::new();
        stream.next_chunk(sender);
        reply.wait_blocking()
    }

    fn add_books(database: &SqliteDatabase, count: usize) {
        for number in 0..count {
            execute(
                database,
                "INSERT INTO book (title) VALUES (%s)",
                vec![Value::Text(format!("Book {number}"))],
            )
            .unwrap();
        }
    }

    #[test]
    fn a_stream_hands_out_its_rows_chunk_by_chunk() {
        let (_folder, database) = open(1);
        add_books(&database, 5);
        let (stream, requests) = djox_core::RowStream::new();
        database.stream_rows("SELECT id FROM book ORDER BY id".into(), vec![], 2, requests);
        let mut sizes = Vec::new();
        loop {
            let chunk = next_chunk(&stream).unwrap();
            sizes.push(chunk.rows.len());
            if chunk.rows.len() < 2 {
                break;
            }
        }
        assert_eq!(sizes, [2, 2, 1]);
        assert_eq!(next_chunk(&stream).unwrap_err(), Error::Closed, "nothing after the end");
    }

    #[test]
    fn other_statements_run_while_a_stream_waits() {
        // One reader only: the stream must not take it.
        let (_folder, database) = open(1);
        add_books(&database, 3);
        let (stream, requests) = djox_core::RowStream::new();
        database.stream_rows("SELECT id FROM book".into(), vec![], 1, requests);
        assert_eq!(next_chunk(&stream).unwrap().rows.len(), 1);
        assert_eq!(count_books(&database), Value::Integer(3));
        add_books(&database, 1);
        assert_eq!(count_books(&database), Value::Integer(4));
        drop(stream); // stopping early lets the stream's connection go
    }

    #[test]
    fn a_stream_refuses_writes_and_reports_errors() {
        let (_folder, database) = open(1);
        let (stream, requests) = djox_core::RowStream::new();
        database.stream_rows("DELETE FROM book".into(), vec![], 10, requests);
        assert!(matches!(next_chunk(&stream), Err(Error::Programming(_))));
        let (stream, requests) = djox_core::RowStream::new();
        database.stream_rows("SELECT nothing FROM book".into(), vec![], 10, requests);
        assert!(next_chunk(&stream).is_err());
    }

    #[test]
    fn a_closed_database_streams_nothing() {
        let (_folder, database) = open(1);
        database.close();
        let (stream, requests) = djox_core::RowStream::new();
        database.stream_rows("SELECT 1".into(), vec![], 10, requests);
        assert_eq!(next_chunk(&stream).unwrap_err(), Error::Closed);
    }
}
