//! A worker: a thread that owns one SQLite connection and runs the
//! statements in its queue, one at a time, in order.
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

/// Where a worker takes its jobs from.
pub(crate) enum Jobs {
    /// The writer's own queue.
    Writer(Receiver<Job>),
    /// The queue all readers share. `Arc` is a shared pointer, like a Python
    /// reference: every reader holds one to the same queue.
    Readers(Arc<ReaderQueue>),
}

impl Jobs {
    /// Waits for the next job. `None` means the queue is closed and empty.
    fn next(&self) -> Option<Job> {
        match self {
            // `.ok()` turns "the queue is closed" into `None`.
            Jobs::Writer(receiver) => receiver.recv().ok(),
            Jobs::Readers(queue) => queue.next_job(),
        }
    }
}

/// Starts a thread that runs the `jobs` queue on `connection`.
///
/// The thread stops once the queue is closed (which is what closing the
/// database does) *and* empty, so statements already sent
/// still run. Then the connection closes.
///
/// A reader also gets the writer's queue as `writer`: a statement that turns
/// out to write (like `INSERT ... RETURNING` sent to `fetch_all`) is passed on
/// to the writer instead of failing.
pub(crate) fn start_worker(
    name: String,
    mut connection: SqliteConnection,
    jobs: Jobs,
    writer: Option<Sender<Job>>,
) -> std::io::Result<()> {
    // `move` hands `connection`, `jobs` and `writer` over to the new thread:
    // from now on only that thread can use them.
    thread::Builder::new().name(name).spawn(move || {
        // `while let Some(job) = ...` loops for as long as there is a next job.
        while let Some(job) = jobs.next() {
            run_job(&mut connection, job, writer.as_ref());
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
