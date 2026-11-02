//! Streams: the rows of one query, read a chunk at a time on a connection of
//! the stream's own.
//!
//! Postgres can read a query's rows bit by bit with a *portal* (a
//! server-side cursor, what Django's `iterator()` uses through psycopg):
//! the query runs once, and each "give me the next 2000 rows" sends that
//! many over. A portal lives inside a transaction, so the stream opens a
//! read-only one, which also refuses a statement that writes.
//!
//! The stream doesn't borrow a pooled connection: a caller that reads slowly
//! (or never finishes) would hold it, and other statements would wait for
//! the pool. It opens a connection of its own, on a thread of its own, and
//! closes both when the stream ends or the caller drops its `RowStream`.

use djox_core::{ChunkRequests, Error, Result, Rows, Value};
use tokio_postgres_rustls::MakeRustlsConnect;

use super::database::{parameter_references, rows_to_djox, wrap_parameters};
use super::errors::to_djox_error;
use super::placeholders::to_numbered_placeholders;
use super::settings::PostgresSettings;

/// Starts a thread that runs `sql` and answers each chunk `requests` asks for.
///
/// If the thread can't start, `requests` is dropped with it, and the first
/// chunk is answered with `Error::Closed`.
pub(super) fn start_stream(
    settings: &PostgresSettings,
    tls_connector: MakeRustlsConnect,
    sql: String,
    parameters: Vec<Value>,
    chunk_size: usize,
    requests: ChunkRequests,
) {
    let settings = settings.clone();
    let _ = std::thread::Builder::new()
        .name("djox-postgres-stream".to_string())
        .spawn(move || run_stream(&settings, tls_connector, &sql, &parameters, chunk_size, &requests));
}

/// The stream's thread: open, then answer chunk after chunk.
fn run_stream(
    settings: &PostgresSettings,
    tls_connector: MakeRustlsConnect,
    sql: &str,
    parameters: &[Value],
    chunk_size: usize,
    requests: &ChunkRequests,
) {
    // A runtime of the thread's own, running on this one thread. Asking for a
    // chunk (`requests.next()`) blocks the thread, which async code must not
    // do; here it is fine, since nothing else runs on this runtime.
    let runtime = match tokio::runtime::Builder::new_current_thread().enable_all().build() {
        Ok(runtime) => runtime,
        Err(error) => {
            answer_first_request(requests, Error::Operational(error.to_string()));
            return;
        }
    };
    let config = match settings.driver_config() {
        Ok(config) => config,
        Err(error) => {
            answer_first_request(requests, error);
            return;
        }
    };
    let connected = runtime.block_on(config.connect(tls_connector));
    let (mut client, connection) = match connected {
        Ok(pair) => pair,
        Err(error) => {
            answer_first_request(requests, to_djox_error(error));
            return;
        }
    };
    // The connection's own task reads and writes the socket; it runs
    // whenever `block_on` below runs, and ends when the client is dropped.
    runtime.spawn(connection);

    // Read-only: a statement that writes fails, as streams only read.
    let started = runtime.block_on(client.build_transaction().read_only(true).start());
    let transaction = match started {
        Ok(transaction) => transaction,
        Err(error) => {
            answer_first_request(requests, to_djox_error(error));
            return;
        }
    };
    let wrapped = wrap_parameters(parameters);
    let opened = runtime.block_on(async {
        let statement = transaction.prepare(&to_numbered_placeholders(sql)).await?;
        let portal = transaction.bind(&statement, &parameter_references(&wrapped)).await?;
        // `Ok::<_, tokio_postgres::Error>` names the error type the `?`s above return.
        Ok::<_, tokio_postgres::Error>((statement, portal))
    });
    let (statement, portal) = match opened {
        Ok(opened) => opened,
        Err(error) => {
            answer_first_request(requests, stream_error(to_djox_error(error)));
            return;
        }
    };

    // `i32`: the protocol's number type for "how many rows".
    let rows_per_chunk = i32::try_from(chunk_size).unwrap_or(i32::MAX);
    // `while let Some(...)`: until the caller drops its `RowStream`.
    while let Some(reply) = requests.next() {
        let chunk: Result<Rows> = runtime
            .block_on(transaction.query_portal(&portal, rows_per_chunk))
            .map_err(|error| stream_error(to_djox_error(error)))
            .and_then(|rows| rows_to_djox(&statement, rows));
        let is_last = match &chunk {
            Ok(rows) => rows.rows.len() < chunk_size,
            Err(_) => true,
        };
        reply.send(chunk);
        if is_last {
            break;
        }
    }
    // Leaving the function drops the transaction (rolled back: it only read),
    // the client (closing the connection) and the runtime.
}

/// Answers the first chunk the caller asks for with `error`.
fn answer_first_request(requests: &ChunkRequests, error: Error) {
    if let Some(reply) = requests.next() {
        reply.send(Err(error));
    }
}

/// A write refused by the stream's read-only transaction is the caller's
/// mistake (SQLSTATE `25006`, sorted as `Internal` elsewhere).
fn stream_error(error: Error) -> Error {
    match error {
        Error::Internal(message) if message.contains("read-only transaction") => {
            Error::Programming(format!("only queries that read can be streamed ({message})"))
        }
        other => other,
    }
}
