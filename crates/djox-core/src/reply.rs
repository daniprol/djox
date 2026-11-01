//! How a backend hands back the answer to a statement.
//!
//! Backends run statements on other threads, so the answer arrives later. The
//! caller gives the backend a [`ReplySender`]: a function to call once with
//! the answer, like the callback of `loop.call_soon_threadsafe`. The Python
//! extension's callback completes an `asyncio.Future` directly, the fastest
//! way back into Python. Rust code that would rather `await` the answer uses
//! [`Reply::new`], which pairs a sender with a [`Reply`] to wait on.

use tokio::sync::oneshot;

use crate::{Error, Result};

/// The function that receives the answer to one statement.
///
/// It is always called **exactly once**: with the answer, or with
/// [`Error::Closed`] if the statement was dropped without running (its
/// connection went away). So whoever waits is never left waiting forever.
pub struct ReplySender<T> {
    /// `Box<dyn FnOnce(...)>` is "any function, called at most once", stored
    /// on the heap, like a Python callable. `Send` lets it move to the worker
    /// thread. It is an `Option` so we can tell whether it was called yet:
    /// calling it takes it out and leaves `None`.
    callback: Option<Box<dyn FnOnce(Result<T>) + Send>>,
}

impl<T> ReplySender<T> {
    /// A sender that calls `callback` with the answer.
    ///
    /// `'static` means the function doesn't borrow anything that could
    /// disappear before it is called.
    pub fn new(callback: impl FnOnce(Result<T>) + Send + 'static) -> ReplySender<T> {
        ReplySender {
            callback: Some(Box::new(callback)),
        }
    }

    /// Hands over the answer.
    pub fn send(mut self, result: Result<T>) {
        // `.take()` moves the function out of the `Option`, so `drop` below won't call it again.
        if let Some(callback) = self.callback.take() {
            callback(result);
        }
    }
}

/// `Drop` runs when a value goes away (like `__del__`, but always and right
/// away). A sender dropped without answering answers [`Error::Closed`].
impl<T> Drop for ReplySender<T> {
    fn drop(&mut self) {
        if let Some(callback) = self.callback.take() {
            callback(Err(Error::Closed));
        }
    }
}

/// An answer to wait for from async Rust code: `reply.wait().await`.
///
/// Inside it is a *oneshot channel*: a pipe that carries exactly one message.
pub struct Reply<T> {
    receiver: oneshot::Receiver<Result<T>>,
}

impl<T: Send + 'static> Reply<T> {
    /// A sender to give to the backend, and the [`Reply`] it will fill in.
    ///
    /// `(ReplySender<T>, Reply<T>)` is a tuple, like returning `sender, reply` in Python.
    pub fn new() -> (ReplySender<T>, Reply<T>) {
        let (channel_sender, receiver) = oneshot::channel();
        let sender = ReplySender::new(move |result| {
            // `let _ =` ignores a failed send: it only fails if nobody waits any more.
            let _ = channel_sender.send(result);
        });
        (sender, Reply { receiver })
    }

    /// Waits for the answer without blocking the thread.
    pub async fn wait(self) -> Result<T> {
        self.receiver.await.unwrap_or(Err(Error::Closed))
    }

    /// Waits for the answer and blocks the thread meanwhile. For tests and
    /// for code with no async runtime. Never call it from async code.
    pub fn wait_blocking(self) -> Result<T> {
        self.receiver.blocking_recv().unwrap_or(Err(Error::Closed))
    }
}
