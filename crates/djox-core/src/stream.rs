//! [`RowStream`]: the rows of a big query, handed over a chunk at a time.
//!
//! [`crate::Database::fetch_all`] collects every row before answering. For a
//! query with a million rows, that is a lot of memory at once. A stream
//! answers in chunks instead, each one when the caller asks for it, like
//! Django's `iterator()` reads from a server-side cursor:
//!
//! ```text
//!   caller                          backend (its own connection)
//!   ──────                          ────────────────────────────
//!   stream.next_chunk(reply) ──►    runs the query, reads 2000 rows ──► reply
//!   stream.next_chunk(reply) ──►    reads the next 2000 rows        ──► reply
//!   ...                             fewer than 2000: the end
//! ```
//!
//! The caller holds the [`RowStream`]; the backend holds the matching
//! [`ChunkRequests`]. They are the two ends of a channel (a thread-safe pipe,
//! like `queue.Queue`) that carries one [`ReplySender`] per chunk asked for.
//! Dropping the `RowStream` (the caller stopped early) closes the channel, and
//! the backend stops and lets its connection go.

use std::sync::mpsc;

use crate::{ReplySender, Rows};

/// The caller's end of a stream: asks for the next chunk of rows.
pub struct RowStream {
    requests: mpsc::Sender<ReplySender<Rows>>,
}

/// The backend's end of a stream: the chunks the caller asks for, in order.
pub struct ChunkRequests {
    requests: mpsc::Receiver<ReplySender<Rows>>,
}

impl RowStream {
    /// A new stream: the [`RowStream`] for the caller and the
    /// [`ChunkRequests`] to give to the backend
    /// ([`crate::Database::stream_rows`]).
    pub fn new() -> (RowStream, ChunkRequests) {
        let (sender, receiver) = mpsc::channel();
        (RowStream { requests: sender }, ChunkRequests { requests: receiver })
    }

    /// Asks for the next chunk. `reply` gets up to `chunk_size` rows; fewer
    /// (or none) means the query has no more. After the end, `reply` gets
    /// [`crate::Error::Closed`]: ask no more.
    pub fn next_chunk(&self, reply: ReplySender<Rows>) {
        // If the backend is gone, `send` hands `reply` back, and dropping it
        // answers `Error::Closed` (see `ReplySender`'s `Drop`).
        let _ = self.requests.send(reply);
    }
}

impl ChunkRequests {
    /// Waits for the caller to ask for the next chunk. `None` means the caller
    /// dropped its [`RowStream`]: it wants no more rows.
    pub fn next(&self) -> Option<ReplySender<Rows>> {
        // `.ok()` turns "the channel is closed" into `None`.
        self.requests.recv().ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Error, Result};

    /// A `ReplySender` that puts its answer in a channel, to read it in the test.
    fn reply_into_channel() -> (ReplySender<Rows>, mpsc::Receiver<Result<Rows>>) {
        let (sender, receiver) = mpsc::channel();
        let reply = ReplySender::new(move |answer| {
            let _ = sender.send(answer);
        });
        (reply, receiver)
    }

    #[test]
    fn each_request_reaches_the_backend() {
        let (stream, requests) = RowStream::new();
        let (reply, answer) = reply_into_channel();
        stream.next_chunk(reply);
        requests.next().unwrap().send(Ok(Rows::default()));
        assert_eq!(answer.recv().unwrap().unwrap(), Rows::default());
    }

    #[test]
    fn a_dropped_stream_tells_the_backend_to_stop() {
        let (stream, requests) = RowStream::new();
        drop(stream);
        assert!(requests.next().is_none());
    }

    #[test]
    fn asking_after_the_backend_stopped_answers_closed() {
        let (stream, requests) = RowStream::new();
        drop(requests);
        let (reply, answer) = reply_into_channel();
        stream.next_chunk(reply);
        assert!(matches!(answer.recv().unwrap(), Err(Error::Closed)));
    }
}
