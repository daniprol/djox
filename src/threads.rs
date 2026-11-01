//! Taking the GIL from djox's own threads, quickly.
//!
//! Python only lets a thread run Python code once the thread is *registered*
//! with it: Python gives the thread a "thread state" (its own frame stack,
//! exception state and so on). Threads that Python started are registered
//! already. djox's database workers are Rust threads, so they are not.
//!
//! `Python::attach` handles that for us, but in the most cautious way: on a
//! thread that isn't registered, it registers the thread, takes the GIL, and
//! *unregisters the thread again* when it is done. A worker answers every
//! query this way, so it paid for a brand new thread state on every query.
//! That cost about 60 µs per query on a small cloud VM, more than a thread
//! round trip and fifteen times the query itself.
//!
//! [`attach`] registers a djox thread once, the first time it needs Python,
//! and keeps it registered for the rest of its life. Each later call only
//! takes the GIL. Python frees the thread state when the interpreter exits.

use pyo3::ffi;
use pyo3::prelude::*;

/// Runs `work` with the GIL held, like `Python::attach`, but keeps this
/// thread registered with Python afterwards (see the module docs).
///
/// Use it for code that runs on djox's worker threads: answering a query, or
/// a Python function that SQLite calls in the middle of a statement.
pub fn attach<Work, Output>(work: Work) -> Output
where
    // `Work` is any function that takes the GIL token `py` and returns an `Output`.
    Work: for<'py> FnOnce(Python<'py>) -> Output,
{
    stay_registered_with_python();
    Python::attach(work)
}

/// Registers the current thread with Python for good, unless it already is.
fn stay_registered_with_python() {
    // `unsafe` marks calls into Python's C API that Rust can't check. Each
    // one is allowed here, as Python's docs say:
    // - `PyGILState_GetThisThreadState` only reads which thread state belongs
    //   to this thread (none for a thread Python never saw). It doesn't need
    //   the GIL.
    // - `PyGILState_Ensure` registers the thread and takes the GIL.
    // - `PyEval_SaveThread` lets the GIL go but keeps the registration, like
    //   leaving a `with gil:` block without saying goodbye.
    //
    // The check matters: a Python thread (the event loop's, say) can get here
    // too, for example when a query is sent to a database that is already
    // closed. That thread is registered already and may be holding the GIL,
    // which `PyEval_SaveThread` would let go of in the middle of its work.
    unsafe {
        if ffi::PyGILState_GetThisThreadState().is_null() {
            ffi::PyGILState_Ensure();
            ffi::PyEval_SaveThread();
        }
    }
}
