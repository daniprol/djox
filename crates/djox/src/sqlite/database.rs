//! [`SqliteDatabase`]: opens the connections, starts the workers and sends
//! each statement to the right queue.

use std::sync::Mutex;

use crossbeam_channel::Sender;
use djox_core::{Database, Error, Executed, ReplySender, Result, Rows, Value};

use super::connection::{Access, SqliteConnection};
use super::settings::SqliteSettings;
use super::worker::{Job, start_worker};

/// A SQLite database with one writer connection and a few reader
/// connections, each on its own thread (see the module docs for why).
///
/// - `execute` and `execute_many` go to the writer, one at a time, in the
///   order they were sent, so they never fail with "database is locked".
/// - `fetch_all` goes to whichever reader is free. A statement that writes
///   (like `INSERT ... RETURNING`) is passed on to the writer.
pub struct SqliteDatabase {
    /// The two queues, or `None` once the database is closed.
    ///
    /// `Option<T>` is Rust's "`T` or `None`". The `Mutex` (like
    /// `threading.Lock`) lets many threads read and change it safely: each
    /// one locks it just long enough to copy a queue sender.
    queues: Mutex<Option<Queues>>,
    settings: SqliteSettings,
}

/// The writer's queue, and the queue all readers share.
struct Queues {
    writer: Sender<Job>,
    readers: Sender<Job>,
}

impl SqliteDatabase {
    /// Opens the writer connection and the reader connections, and starts a
    /// worker thread for each.
    ///
    /// The writer opens first: it creates the file and turns on WAL mode
    /// before the readers open it.
    pub fn open(settings: SqliteSettings) -> Result<SqliteDatabase> {
        settings.check()?;
        let writer_connection = SqliteConnection::open(&settings, Access::Writer)?;
        let mut reader_connections = Vec::new();
        for _ in 0..settings.reader_count {
            reader_connections.push(SqliteConnection::open(&settings, Access::Reader)?);
        }

        // An unbounded channel is like Python's `queue.SimpleQueue`: sending never waits.
        let (writer_queue, writer_jobs) = crossbeam_channel::unbounded();
        start_worker("djox-sqlite-writer".to_string(), writer_connection, writer_jobs, None)
            .map_err(could_not_start_thread)?;

        let readers_queue = if settings.reader_count == 0 {
            // No readers: the writer runs the reads too.
            writer_queue.clone()
        } else {
            let (readers_queue, reader_jobs) = crossbeam_channel::unbounded();
            // `enumerate()` is Python's `enumerate()`.
            for (number, connection) in reader_connections.into_iter().enumerate() {
                let name = format!("djox-sqlite-reader-{number}");
                start_worker(name, connection, reader_jobs.clone(), Some(writer_queue.clone()))
                    .map_err(could_not_start_thread)?;
            }
            readers_queue
        };

        let queues = Queues {
            writer: writer_queue,
            readers: readers_queue,
        };
        Ok(SqliteDatabase {
            queues: Mutex::new(Some(queues)),
            settings,
        })
    }

    /// The settings the database was opened with.
    pub fn settings(&self) -> &SqliteSettings {
        &self.settings
    }

    /// Puts a job in the writer's queue.
    fn send_to_writer(&self, job: Job) {
        // `.lock().unwrap()` takes the lock; `unwrap` would only fail if another
        // thread crashed while holding it. `.map(...)` copies the sender if the
        // queues are there, and gives `None` if the database is closed.
        let queue = self.queues.lock().unwrap().as_ref().map(|queues| queues.writer.clone());
        send(queue, job);
    }

    /// Puts a job in the readers' queue.
    fn send_to_readers(&self, job: Job) {
        let queue = self
            .queues
            .lock()
            .unwrap()
            .as_ref()
            .map(|queues| queues.readers.clone());
        send(queue, job);
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
        self.send_to_readers(Job::FetchAll { sql, parameters, reply });
    }

    fn execute(&self, sql: String, parameters: Vec<Value>, reply: ReplySender<Executed>) {
        self.send_to_writer(Job::Execute { sql, parameters, reply });
    }

    fn execute_many(&self, sql: String, parameter_lists: Vec<Vec<Value>>, reply: ReplySender<Executed>) {
        self.send_to_writer(Job::ExecuteMany {
            sql,
            parameter_lists,
            reply,
        });
    }

    fn close(&self) {
        // Dropping the queue senders closes the queues. Each worker finishes
        // what is already queued, then stops and closes its connection.
        self.queues.lock().unwrap().take();
    }

    fn is_closed(&self) -> bool {
        self.queues.lock().unwrap().is_none()
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
}
