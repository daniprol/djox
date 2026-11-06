//! A worker: a thread that runs the statements in its queue on its SQLite
//! connection, one at a time, in order. A reader owns its connection; the
//! writer shares its own with quick writes and transaction blocks (see
//! `writer.rs`).
//!
//! In Python terms, a worker is a `ThreadPoolExecutor(max_workers=1)` that
//! keeps its own database connection open. The writer's queue is a
//! *channel*: a thread-safe pipe, like `queue.Queue`, with senders on one end
//! and the worker on the other. The readers share a [`ReaderQueue`].

use std::sync::Arc;
use std::thread;

use crossbeam_channel::{Receiver, Sender};
use djox_core::{Executed, ReplySender, Rows, Value};

use super::connection::SqliteConnection;
use super::readers::ReaderQueue;
use super::writer::SharedWriter;

/// One statement for a worker, with the [`ReplySender`] to answer it.
///
/// A job that is dropped without running (its queue was closed) answers
/// `Error::Closed` on its own: see `ReplySender`'s `Drop`.
pub(crate) enum Job {
    /// Run a statement and return its rows.
    FetchAll {
        sql: String,
        parameters: Vec<Value>,
        reply: ReplySender<Rows>,
    },
    /// Run a statement that changes data.
    Execute {
        sql: String,
        parameters: Vec<Value>,
        reply: ReplySender<Executed>,
    },
    /// Run a statement once per list of parameters, in one transaction.
    ExecuteMany {
        sql: String,
        parameter_lists: Vec<Vec<Value>>,
        reply: ReplySender<Executed>,
    },
}

impl Job {
    /// Answers the job with `error`, without running it.
    fn fail(self, error: djox_core::Error) {
        match self {
            Job::FetchAll { reply, .. } => reply.send(Err(error)),
            Job::Execute { reply, .. } | Job::ExecuteMany { reply, .. } => reply.send(Err(error)),
        }
    }
}

/// Starts the writer's thread: it runs the jobs of the writer's queue on the
/// shared writer connection, each once no transaction is open on it (see
/// `writer.rs`).
///
/// The thread stops once the queue is closed (which is what closing the
/// database does) *and* empty, so statements already sent still run.
pub(crate) fn start_writer(writer: Arc<SharedWriter>, jobs: Receiver<Job>) -> std::io::Result<()> {
    thread::Builder::new()
        .name("djox-sqlite-writer".to_string())
        .spawn(move || {
            // `recv()` fails once the queue is closed and empty, which ends the loop.
            while let Ok(job) = jobs.recv() {
                match writer.connection_for_a_queued_job() {
                    Ok(mut writing) => {
                        run_job(&mut writing.connection, job, None);
                        writer.finish_queued_job(&mut writing);
                    }
                    Err(error) => job.fail(error),
                }
            }
        })?;
    Ok(())
}

/// Starts a reader's thread: it runs the jobs of the readers' queue on `connection`.
///
/// The thread stops once the queue is closed (which is what closing the
/// database does) *and* empty, so statements already sent
/// still run. Then the connection closes.
///
/// A reader also gets the writer's queue as `writer`: a statement that turns
/// out to write (like `INSERT ... RETURNING` sent to `fetch_all`) is passed on
/// to the writer instead of failing.
pub(crate) fn start_reader(
    name: String,
    mut connection: SqliteConnection,
    jobs: Arc<ReaderQueue>,
    writer: Sender<Job>,
) -> std::io::Result<()> {
    // `move` hands `connection`, `jobs` and `writer` over to the new thread:
    // from now on only that thread can use them.
    thread::Builder::new().name(name).spawn(move || {
        // `while let Some(job) = ...` loops for as long as there is a next job.
        while let Some(job) = jobs.next_job() {
            run_job(&mut connection, job, Some(&writer));
        }
    })?;
    Ok(())
}

/// Runs one job on the connection and sends the answer back.
fn run_job(connection: &mut SqliteConnection, job: Job, writer: Option<&Sender<Job>>) {
    match job {
        Job::FetchAll { sql, parameters, reply } => {
            if let Some(writer) = writer {
                // A reader asks SQLite whether the statement writes before running it.
                match connection.statement_writes(&sql) {
                    Ok(true) => {
                        // If the writer is gone, `send` hands the job back and it is dropped:
                        // the caller gets `Error::Closed`.
                        let _ = writer.send(Job::FetchAll { sql, parameters, reply });
                        return;
                    }
                    Ok(false) => {}
                    Err(error) => {
                        reply.send(Err(error));
                        return;
                    }
                }
            }
            reply.send(connection.query(&sql, &parameters));
        }
        Job::Execute { sql, parameters, reply } => {
            reply.send(connection.execute(&sql, &parameters));
        }
        Job::ExecuteMany {
            sql,
            parameter_lists,
            reply,
        } => {
            reply.send(connection.execute_many(&sql, &parameter_lists));
        }
    }
}
