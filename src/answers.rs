//! Turning a backend's answer into a finished `asyncio.Future`.
//!
//! Each query method creates a Future on the running event loop and gives the
//! backend a `ReplySender` whose callback finishes it. The callback runs on the
//! database's worker thread as soon as the statement is done: it converts the
//! answer to Python objects and asks the event loop to set the Future's result
//! (`loop.call_soon_threadsafe`). That is the shortest way back into Python:
//! no extra thread in between.

use djox::{ReplySender, Result};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;

use crate::errors::to_python_error;
use crate::threads;

/// `djox._futures.set_result` and `set_exception`, looked up once.
///
/// `static` is a global variable; `PyOnceLock` fills it on first use, like a
/// module-level cache.
static SET_RESULT: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static SET_EXCEPTION: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static GET_RUNNING_LOOP: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// A new Future on the running event loop, and the sender that finishes it.
///
/// `to_python` converts the answer into a Python object; it runs with the GIL
/// held, on the worker thread. `T: Send + 'static` means the answer can move
/// between threads and borrows nothing.
pub fn future_for_answer<'py, T, ToPython>(
    py: Python<'py>,
    to_python: ToPython,
) -> PyResult<(Bound<'py, PyAny>, ReplySender<T>)>
where
    T: Send + 'static,
    ToPython: for<'any> FnOnce(Python<'any>, T) -> PyResult<Bound<'any, PyAny>> + Send + 'static,
{
    future_answered_by(py, to_python, AnsweringThread::Worker)
}

/// Like [`future_for_answer`], for an answer that comes from a short-lived
/// thread (a stream's): that thread isn't kept registered with Python after
/// answering, since it will soon end (see `threads.rs`).
pub fn future_for_stream_answer<'py, T, ToPython>(
    py: Python<'py>,
    to_python: ToPython,
) -> PyResult<(Bound<'py, PyAny>, ReplySender<T>)>
where
    T: Send + 'static,
    ToPython: for<'any> FnOnce(Python<'any>, T) -> PyResult<Bound<'any, PyAny>> + Send + 'static,
{
    future_answered_by(py, to_python, AnsweringThread::ShortLived)
}

/// Which kind of thread finishes a Future.
#[derive(Clone, Copy)] // `Copy`: a plain value, copied like an `int` in Python.
enum AnsweringThread {
    /// One of the database's workers, alive as long as the database.
    Worker,
    /// A thread that ends soon after answering.
    ShortLived,
}

fn future_answered_by<'py, T, ToPython>(
    py: Python<'py>,
    to_python: ToPython,
    answering_thread: AnsweringThread,
) -> PyResult<(Bound<'py, PyAny>, ReplySender<T>)>
where
    T: Send + 'static,
    ToPython: for<'any> FnOnce(Python<'any>, T) -> PyResult<Bound<'any, PyAny>> + Send + 'static,
{
    let get_running_loop = GET_RUNNING_LOOP.import(py, "asyncio", "get_running_loop")?;
    let event_loop = get_running_loop.call0()?;
    let future = event_loop.call_method0("create_future")?;

    let event_loop = event_loop.unbind();
    let unbound_future = future.clone().unbind();
    let sender = ReplySender::new(move |answer: Result<T>| {
        let finish = |py: Python<'_>| {
            let (finish, value) = match answer.map_err(to_python_error).and_then(|value| to_python(py, value)) {
                Ok(value) => (SET_RESULT.import(py, "djox._futures", "set_result"), value),
                Err(error) => (
                    SET_EXCEPTION.import(py, "djox._futures", "set_exception"),
                    error.into_value(py).into_bound(py).into_any(),
                ),
            };
            // If the event loop is already closed there is nobody to tell: ignore the error.
            if let Ok(finish) = finish {
                let _ = event_loop.call_method1(py, "call_soon_threadsafe", (finish, unbound_future, value));
            }
        };
        // Takes the GIL for this thread, like a `with gil:` block. `threads::attach`
        // keeps a worker thread registered with Python between answers, which
        // is much faster (see `threads.rs`); a short-lived thread isn't kept.
        match answering_thread {
            AnsweringThread::Worker => threads::attach(finish),
            AnsweringThread::ShortLived => Python::attach(finish),
        }
    });
    Ok((future, sender))
}
