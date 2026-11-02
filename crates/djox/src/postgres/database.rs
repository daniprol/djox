//! [`PostgresDatabase`]: a pool of connections and the async runtime they run on.

use std::future::Future;
use std::sync::Mutex;
use std::time::Duration;

use deadpool_postgres::{Manager, ManagerConfig, Pool, PoolError, RecyclingMethod};
use djox_core::{ChunkRequests, Database, Error, Executed, ReplySender, Result, Rows, Value};
use tokio_postgres::types::ToSql;
use tokio_postgres::{Row, Statement};
use tokio_postgres_rustls::MakeRustlsConnect;
use tokio_util::task::TaskTracker;

use super::errors::to_djox_error;
use super::placeholders::to_numbered_placeholders;
use super::settings::PostgresSettings;
use super::stream::start_stream;
use super::values::{Column, Parameter};

/// How many statements a connection keeps prepared. Past that the cache
/// starts over, so a program that writes new SQL all the time (an `IN`
/// list of a different length each time, say) doesn't grow it forever.
const MOST_PREPARED_STATEMENTS: usize = 500;

/// A Postgres database: a pool of connections, each statement on whichever
/// connection is free (see the module docs).
pub struct PostgresDatabase {
    /// The runtime, the pool and the statements on their way, or `None` once
    /// the database is closed. (`Mutex`: like `threading.Lock`.)
    running: Mutex<Option<Running>>,
    settings: PostgresSettings,
    /// What opens TLS connections, made once (it reads certificate files)
    /// and shared by the pool and the streams.
    tls_connector: MakeRustlsConnect,
}

/// What an open database runs on.
struct Running {
    /// The tokio runtime: a couple of threads that run every connection and
    /// statement, like an asyncio event loop running in the background.
    runtime: tokio::runtime::Runtime,
    pool: Pool,
    /// The statements sent and not answered yet, so `close()` can wait for them.
    statements: TaskTracker,
}

impl PostgresDatabase {
    /// Starts the runtime and the pool, and opens one connection to check
    /// that the server answers and the settings work.
    ///
    /// It blocks until that connection is open: call it with the GIL released.
    pub fn open(settings: PostgresSettings) -> Result<PostgresDatabase> {
        settings.check()?;
        let runtime = tokio::runtime::Builder::new_multi_thread()
            // Two threads are plenty: they only wait for the network.
            .worker_threads(2)
            .thread_name("djox-postgres")
            .enable_all() // timers and network sockets
            .build()
            .map_err(|error| Error::Operational(format!("could not start djox's Postgres threads: {error}")))?;

        // `Fast` recycling: a connection given back to the pool is reused as
        // it is, unless the driver saw it close.
        let manager_config = ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        };
        // The TLS connector is used only when `sslmode` asks for TLS.
        let tls_connector = settings.tls_connector()?;
        let manager = Manager::from_config(settings.driver_config()?, tls_connector.clone(), manager_config);
        let pool = Pool::builder(manager)
            .max_size(settings.pool_size)
            .runtime(deadpool_postgres::Runtime::Tokio1)
            .build()
            .map_err(|error| Error::Settings(format!("could not make the Postgres pool: {error}")))?;

        // `block_on` runs async code to the end on this thread, like `asyncio.run()`.
        // The connection goes back to the pool when `checked` is dropped.
        let checked = runtime.block_on(pool.get()).map_err(pool_error)?;
        drop(checked);

        let running = Running {
            runtime,
            pool,
            statements: TaskTracker::new(),
        };
        Ok(PostgresDatabase {
            running: Mutex::new(Some(running)),
            settings,
            tls_connector,
        })
    }

    /// The settings the database was opened with.
    pub fn settings(&self) -> &PostgresSettings {
        &self.settings
    }

    /// The pool, or `None` if the database is closed.
    fn pool(&self) -> Option<Pool> {
        // `.map(...)` copies the pool if the database is open. A `Pool` is a
        // shared pointer inside, so the copy is cheap and uses the same connections.
        self.running
            .lock()
            .unwrap()
            .as_ref()
            .map(|running| running.pool.clone())
    }

    /// Runs `statement` on the runtime, as a task of its own, like
    /// `asyncio.create_task()`. If the database is closed, `statement` is
    /// dropped without running, and the `ReplySender` inside it answers
    /// `Error::Closed`.
    ///
    /// `impl Future<Output = ()>` is "any async block that returns nothing";
    /// `Send + 'static` lets it move to the runtime's threads.
    fn start(&self, statement: impl Future<Output = ()> + Send + 'static) {
        let running = self.running.lock().unwrap();
        if let Some(running) = running.as_ref() {
            running.statements.spawn_on(statement, running.runtime.handle());
        }
    }
}

impl Database for PostgresDatabase {
    fn backend_name(&self) -> &str {
        "postgres"
    }

    fn fetch_all(&self, sql: String, parameters: Vec<Value>, reply: ReplySender<Rows>) {
        let Some(pool) = self.pool() else {
            return; // closed: dropping `reply` answers `Error::Closed`
        };
        // `async move { ... }` is an async block that owns what it uses, like
        // a coroutine object made from a closure. It runs once `start` spawns it.
        self.start(async move {
            reply.send(fetch_all(&pool, &sql, &parameters).await);
        });
    }

    fn stream_rows(&self, sql: String, parameters: Vec<Value>, chunk_size: usize, requests: ChunkRequests) {
        if self.is_closed() {
            return; // dropping `requests` answers the first chunk with `Error::Closed`
        }
        let tls_connector = self.tls_connector.clone();
        start_stream(
            &self.settings,
            tls_connector,
            sql,
            parameters,
            chunk_size.max(1),
            requests,
        );
    }

    fn execute(&self, sql: String, parameters: Vec<Value>, reply: ReplySender<Executed>) {
        let Some(pool) = self.pool() else {
            return;
        };
        self.start(async move {
            reply.send(execute(&pool, &sql, &parameters).await);
        });
    }

    fn execute_many(&self, sql: String, parameter_lists: Vec<Vec<Value>>, reply: ReplySender<Executed>) {
        let Some(pool) = self.pool() else {
            return;
        };
        self.start(async move {
            reply.send(execute_many(&pool, &sql, &parameter_lists).await);
        });
    }

    fn close(&self) {
        let running = self.running.lock().unwrap().take();
        let Some(running) = running else {
            return; // closing twice does nothing
        };
        if tokio::runtime::Handle::try_current().is_ok() {
            // Called from one of the runtime's own threads (by a garbage
            // collection in the middle of an answer, say): it can't wait for itself.
            running.pool.close();
            running.runtime.shutdown_background();
            return;
        }
        // Wait for the statements already sent, then close every connection,
        // then stop the runtime's threads.
        running.statements.close();
        running.runtime.block_on(running.statements.wait());
        running.pool.close();
        running.runtime.shutdown_timeout(Duration::from_secs(5));
    }

    fn is_closed(&self) -> bool {
        self.running.lock().unwrap().is_none()
    }
}

/// Dropping the database (like `__del__`) stops it without waiting: the
/// statements still on their way answer `Error::Closed`. (Python drops it
/// while holding the GIL, which the answers need, so waiting could hang.)
impl Drop for PostgresDatabase {
    fn drop(&mut self) {
        let running = self.running.lock().unwrap().take();
        if let Some(running) = running {
            running.pool.close();
            running.runtime.shutdown_background();
        }
    }
}

/// Runs a query on a pooled connection and returns all its rows.
async fn fetch_all(pool: &Pool, sql: &str, parameters: &[Value]) -> Result<Rows> {
    // `.await` waits without blocking the thread, like in Python. `?` returns the error now.
    let connection = pool.get().await.map_err(pool_error)?;
    let statement = prepare(&connection, sql).await?;
    let wrapped = wrap_parameters(parameters);
    let rows = connection
        .query(&statement, &parameter_references(&wrapped))
        .await
        .map_err(to_djox_error)?;
    rows_to_djox(&statement, rows)
}

/// Runs a statement that changes data on a pooled connection.
async fn execute(pool: &Pool, sql: &str, parameters: &[Value]) -> Result<Executed> {
    let connection = pool.get().await.map_err(pool_error)?;
    let statement = prepare(&connection, sql).await?;
    let wrapped = wrap_parameters(parameters);
    let rows_affected = connection
        .execute(&statement, &parameter_references(&wrapped))
        .await
        .map_err(to_djox_error)?;
    // Postgres has no "last inserted id" of a connection: Django asks for ids with `RETURNING`.
    Ok(Executed {
        rows_affected,
        last_insert_id: 0,
    })
}

/// Runs a statement once per list of parameters, in one transaction on one connection.
async fn execute_many(pool: &Pool, sql: &str, parameter_lists: &[Vec<Value>]) -> Result<Executed> {
    // `mut`: starting a transaction borrows the connection to change it.
    let mut connection = pool.get().await.map_err(pool_error)?;
    let transaction = connection.transaction().await.map_err(to_djox_error)?;
    let statement = transaction
        .prepare_cached(&to_numbered_placeholders(sql))
        .await
        .map_err(to_djox_error)?;
    let mut rows_affected = 0;
    for parameters in parameter_lists {
        let wrapped = wrap_parameters(parameters);
        // On an error, `?` returns early and `transaction` is dropped, which
        // rolls it back: none of the statements are kept.
        rows_affected += transaction
            .execute(&statement, &parameter_references(&wrapped))
            .await
            .map_err(to_djox_error)?;
    }
    transaction.commit().await.map_err(to_djox_error)?;
    Ok(Executed {
        rows_affected,
        last_insert_id: 0,
    })
}

/// The prepared statement for `sql` on this connection: prepared the first
/// time, then taken from the connection's cache.
///
/// Preparing sends the SQL to Postgres once; it answers with the types of
/// the parameters and columns. Later runs send only the parameters: one
/// round trip instead of two.
async fn prepare(connection: &deadpool_postgres::Client, sql: &str) -> Result<Statement> {
    if connection.statement_cache.size() >= MOST_PREPARED_STATEMENTS {
        connection.statement_cache.clear();
    }
    connection
        .prepare_cached(&to_numbered_placeholders(sql))
        .await
        .map_err(to_djox_error)
}

/// Each value wrapped as a [`Parameter`] the driver can send.
pub(super) fn wrap_parameters(parameters: &[Value]) -> Vec<Parameter<'_>> {
    let mut wrapped = Vec::with_capacity(parameters.len());
    for value in parameters {
        wrapped.push(Parameter(value));
    }
    wrapped
}

/// The driver takes parameters as a list of references to "anything it can
/// send" (`&dyn ToSql`), like a list of objects that share a `Protocol`.
pub(super) fn parameter_references<'a>(wrapped: &'a [Parameter<'a>]) -> Vec<&'a (dyn ToSql + Sync)> {
    let mut references: Vec<&(dyn ToSql + Sync)> = Vec::with_capacity(wrapped.len());
    for parameter in wrapped {
        references.push(parameter);
    }
    references
}

/// The driver's rows as djox `Rows`: the column names and every value.
pub(super) fn rows_to_djox(statement: &Statement, rows: Vec<Row>) -> Result<Rows> {
    let mut columns = Vec::new();
    for column in statement.columns() {
        columns.push(column.name().to_string());
    }
    let mut djox_rows = Vec::with_capacity(rows.len());
    for row in rows {
        djox_rows.push(row_to_djox(&row)?);
    }
    Ok(Rows {
        columns,
        rows: djox_rows,
    })
}

/// One row's values, read with [`Column`].
pub(super) fn row_to_djox(row: &Row) -> Result<Vec<Value>> {
    let mut values = Vec::with_capacity(row.len());
    for index in 0..row.len() {
        // `try_get::<_, Column>` reads the column as our `Column` type. `_` lets Rust work out the index type.
        let column: Column = row.try_get(index).map_err(to_djox_error)?;
        values.push(column.0);
    }
    Ok(values)
}

/// A pool error as a djox error.
fn pool_error(error: PoolError) -> Error {
    match error {
        PoolError::Closed => Error::Closed,
        // An error Postgres reported while connecting (a wrong password...) is sorted like any other.
        PoolError::Backend(error) if error.as_db_error().is_some() => to_djox_error(error),
        // The server can't be reached, or a timeout.
        other => Error::Operational(format!("could not connect to Postgres: {other}")),
    }
}

/// These tests need a Postgres server: set `DJOX_POSTGRES_URL`
/// (like `postgresql://postgres:postgres@localhost/postgres`). Without it
/// they pass without checking anything, and say so.
#[cfg(test)]
mod tests {
    use djox_core::{Reply, RowStream};

    use super::*;
    use crate::postgres::TlsMode;

    /// A database on the test server, with an empty table `table` of books,
    /// or `None` when no server is set.
    fn open(table: &str) -> Option<PostgresDatabase> {
        let Ok(url) = std::env::var("DJOX_POSTGRES_URL") else {
            eprintln!("DJOX_POSTGRES_URL is not set: skipping a Postgres test");
            return None;
        };
        let mut settings = PostgresSettings::new(url);
        settings.pool_size = 4;
        let database = PostgresDatabase::open(settings).unwrap();
        execute(&database, &format!("DROP TABLE IF EXISTS {table}"), vec![]).unwrap();
        let create = format!("CREATE TABLE {table} (id serial PRIMARY KEY, title text UNIQUE NOT NULL, pages integer)");
        execute(&database, &create, vec![]).unwrap();
        Some(database)
    }

    fn fetch_all(database: &PostgresDatabase, sql: &str, parameters: Vec<Value>) -> Result<Rows> {
        let (sender, reply) = Reply::new();
        database.fetch_all(sql.to_string(), parameters, sender);
        reply.wait_blocking()
    }

    fn execute(database: &PostgresDatabase, sql: &str, parameters: Vec<Value>) -> Result<Executed> {
        let (sender, reply) = Reply::new();
        database.execute(sql.to_string(), parameters, sender);
        reply.wait_blocking()
    }

    fn text(words: &str) -> Value {
        Value::Text(words.to_string())
    }

    #[test]
    fn runs_statements_with_parameters() {
        let Some(database) = open("rust_book_statements") else {
            return;
        };
        let sql = "INSERT INTO rust_book_statements (title, pages) VALUES (%s, %s)";
        let inserted = execute(&database, sql, vec![text("Dune"), Value::Integer(412)]).unwrap();
        assert_eq!(inserted.rows_affected, 1);
        let rows = fetch_all(
            &database,
            "SELECT title, pages FROM rust_book_statements WHERE pages > %s",
            vec![Value::Integer(100)],
        )
        .unwrap();
        assert_eq!(rows.columns, ["title", "pages"]);
        assert_eq!(rows.rows, [[text("Dune"), Value::Integer(412)]]);
    }

    #[test]
    fn connects_with_verified_tls() {
        // A server with TLS, and the authority that signed its certificate
        // (`scripts/make-test-certificates.sh`).
        let (Ok(url), Ok(root_certificate)) = (
            std::env::var("DJOX_POSTGRES_URL"),
            std::env::var("DJOX_POSTGRES_ROOT_CERT"),
        ) else {
            eprintln!("DJOX_POSTGRES_URL or DJOX_POSTGRES_ROOT_CERT is not set: skipping a TLS test");
            return;
        };
        let mut settings = PostgresSettings::new(url);
        settings.tls.mode = Some(TlsMode::VerifyFull);
        settings.tls.root_certificates = Some(root_certificate);
        let database = PostgresDatabase::open(settings).unwrap();
        let rows = fetch_all(
            &database,
            "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()",
            vec![],
        )
        .unwrap();
        assert_eq!(rows.rows, [[Value::Boolean(true)]]);
    }

    #[test]
    fn reads_typed_values() {
        let Some(database) = open("rust_book_types") else {
            return;
        };
        let rows = fetch_all(
            &database,
            "SELECT 12.50::numeric, true, '2024-05-01'::date, '1 day 2 seconds'::interval, \
             '{\"a\": 1}'::jsonb, '192.0.2.1'::inet, %s::uuid, 'Dune'::varchar, NULL::integer",
            vec![text("a8098c1a-f86e-11da-bd1a-00112444be1e")],
        )
        .unwrap();
        let row = &rows.rows[0];
        assert_eq!(row[0], Value::Decimal("12.50".into()));
        assert_eq!(row[1], Value::Boolean(true));
        assert_eq!(
            row[2],
            Value::Date(chrono::NaiveDate::from_ymd_opt(2024, 5, 1).unwrap())
        );
        assert_eq!(row[3], Value::Duration(chrono::TimeDelta::seconds(86_402)));
        assert_eq!(row[4], text("{\"a\": 1}"));
        assert_eq!(row[5], text("192.0.2.1"));
        assert!(matches!(row[6], Value::Uuid(_)));
        assert_eq!(row[7], text("Dune"));
        assert_eq!(row[8], Value::Null);
    }

    #[test]
    fn sorts_errors_like_psycopg() {
        let Some(database) = open("rust_book_errors") else {
            return;
        };
        let sql = "INSERT INTO rust_book_errors (title) VALUES (%s)";
        execute(&database, sql, vec![text("Dune")]).unwrap();
        assert!(matches!(
            execute(&database, sql, vec![text("Dune")]),
            Err(Error::Integrity(_))
        ));
        assert!(matches!(
            fetch_all(&database, "SELECT nothing FROM rust_book_errors", vec![]),
            Err(Error::Programming(_))
        ));
        let too_big = Value::Integer(1 << 40);
        let update = "UPDATE rust_book_errors SET pages = %s";
        assert!(matches!(execute(&database, update, vec![too_big]), Err(Error::Data(_))));
    }

    #[test]
    fn execute_many_keeps_all_or_nothing() {
        let Some(database) = open("rust_book_many") else { return };
        let sql = "INSERT INTO rust_book_many (title) VALUES (%s)";
        let (sender, reply) = Reply::new();
        database.execute_many(sql.into(), vec![vec![text("A")], vec![text("B")]], sender);
        assert_eq!(reply.wait_blocking().unwrap().rows_affected, 2);
        let (sender, reply) = Reply::new();
        database.execute_many(sql.into(), vec![vec![text("C")], vec![text("A")]], sender);
        assert!(matches!(reply.wait_blocking(), Err(Error::Integrity(_))));
        let count = fetch_all(&database, "SELECT count(*) FROM rust_book_many", vec![]).unwrap();
        assert_eq!(count.rows[0][0], Value::Integer(2));
    }

    #[test]
    fn many_statements_at_once_share_the_pool() {
        let Some(database) = open("rust_book_concurrent") else {
            return;
        };
        let mut writes = Vec::new();
        for number in 0..200 {
            let (sender, write) = Reply::new();
            let title = text(&format!("Book {number}"));
            database.execute(
                "INSERT INTO rust_book_concurrent (title) VALUES (%s)".into(),
                vec![title],
                sender,
            );
            writes.push(write);
        }
        for write in writes {
            assert_eq!(write.wait_blocking().unwrap().rows_affected, 1);
        }
        let count = fetch_all(&database, "SELECT count(*) FROM rust_book_concurrent", vec![]).unwrap();
        assert_eq!(count.rows[0][0], Value::Integer(200));
    }

    fn next_chunk(stream: &RowStream) -> Result<Rows> {
        let (sender, reply) = Reply::new();
        stream.next_chunk(sender);
        reply.wait_blocking()
    }

    #[test]
    fn a_stream_hands_out_its_rows_chunk_by_chunk() {
        let Some(database) = open("rust_book_stream") else {
            return;
        };
        for number in 0..5 {
            let sql = "INSERT INTO rust_book_stream (title) VALUES (%s)";
            execute(&database, sql, vec![text(&format!("Book {number}"))]).unwrap();
        }
        let (stream, requests) = RowStream::new();
        database.stream_rows(
            "SELECT id FROM rust_book_stream ORDER BY id".into(),
            vec![],
            2,
            requests,
        );
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
    fn a_stream_refuses_writes() {
        let Some(database) = open("rust_book_stream_writes") else {
            return;
        };
        let (stream, requests) = RowStream::new();
        database.stream_rows("DELETE FROM rust_book_stream_writes".into(), vec![], 10, requests);
        assert!(matches!(next_chunk(&stream), Err(Error::Programming(_))));
    }

    #[test]
    fn closing_finishes_sent_work_and_refuses_new_work() {
        let Some(database) = open("rust_book_closing") else {
            return;
        };
        let (sender, sent) = Reply::new();
        database.execute(
            "INSERT INTO rust_book_closing (title) VALUES ('Dune')".into(),
            vec![],
            sender,
        );
        database.close();
        assert!(database.is_closed());
        assert_eq!(sent.wait_blocking().unwrap().rows_affected, 1);
        assert_eq!(fetch_all(&database, "SELECT 1", vec![]).unwrap_err(), Error::Closed);
        database.close(); // closing twice does nothing
    }

    #[test]
    fn a_server_that_cant_be_reached_fails_at_open() {
        let settings = PostgresSettings::new("postgresql://nobody@127.0.0.1:1/nothing");
        assert!(matches!(PostgresDatabase::open(settings), Err(Error::Operational(_))));
    }
}
