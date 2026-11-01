"""djox's own threads, for Django's sync code.

Some work can only run as Django's sync code: a transaction (`atomic()` must
run on one thread, with that thread's connection), and Django methods that
djox doesn't run itself yet (`get_or_create()`, `bulk_create()`, deletes that
cascade, `prefetch_related()`, `Model.save()`, a related manager's `aadd()`...).

Django's own async methods send all of that to one shared "sync thread", one
call at a time, for the whole process. djox sends it to a small pool of its
own threads instead, so these calls don't queue behind each other (or behind
Django's other async calls).

Each pooled thread keeps its Django connections open between calls, and
looks after them the way Django looks after a request thread's connections:
once per request (Django's `request_started` and `request_finished`
signals), before its next call, it runs Django's `close_old_connections()`.
That honours `CONN_MAX_AGE` (`0`, Django's default, closes the connection
after each request; `None` keeps it) and `CONN_HEALTH_CHECKS`, and drops a
connection that broke. A program without requests (a script) keeps them open,
like Django does.

| Function               | What it does                                                |
|------------------------|-------------------------------------------------------------|
| `run_django_method()`  | Run a sync Django method on a pooled thread (or in the transaction this code is in) |
| `take_idle_thread()`, `give_back_idle_thread()` | The pool itself (`djox.atomic()` uses it too) |
| `tidy_connections()`   | On a pooled thread: Django's connection upkeep, once per request |
"""

import asyncio
import contextvars
import threading
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from typing import Any, TypeVar

from asgiref.sync import sync_to_async
from django.core.signals import request_finished, request_started
from django.db import close_old_connections
from django.db import connections as django_connections

from djox import _running, _write_turns
from djox.backends.sqlite3.base import DatabaseWrapper

Result = TypeVar("Result")

# Idle threads kept for the next call. More can run at once; the extra ones
# are closed when their call ends.
MAX_IDLE_THREADS = 8

# The most pooled threads that read at once, per event loop. (Writers take
# turns, so at most one writes.) Each thread holds a connection.
MAX_READING_THREADS = 8


async def run_django_method(alias: str, method: Callable[[], Result], writes: bool) -> Result:
    """Run Django's sync `method` off the event loop, and return its result.

    Inside a transaction (a `djox.atomic()` block, or a sync `atomic()` around
    this code), it runs on the transaction's thread, so it joins the
    transaction. Otherwise it runs on a pooled thread of its own. `writes`
    says whether it may write: then it waits to have the write turn of
    `alias`'s file to itself first (see `djox._write_turns`).

    On a database whose `ENGINE` isn't djox's, it runs like Django's own
    async methods do (on Django's sync thread).
    """
    if not isinstance(django_connections[alias], DatabaseWrapper):
        return await sync_to_async(method)()
    if transaction_thread.get() is not None or _running.open_sync_transaction(alias) is not None:
        # `thread_sensitive=True` sends it to the transaction's thread (asgiref
        # finds it from this context, like `sync_to_async()` in your own code).
        return await sync_to_async(method, thread_sensitive=True)()
    if writes:
        async with _write_turns.transaction_alone(alias):
            return await run_on_pooled_thread(method)
    async with reading_slots_of_this_loop():
        return await run_on_pooled_thread(method)


async def run_on_pooled_thread(method: Callable[[], Result]) -> Result:
    """Run `method` on an idle pooled thread, after its connection upkeep."""

    def tidy_then_run() -> Result:
        tidy_connections()
        return method()

    thread = take_idle_thread()
    try:
        # `run_in_executor()` runs the function on `thread` and lets this task
        # await it. `copy_context().run` runs it with this task's context
        # variables, like `sync_to_async()` does.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(thread, contextvars.copy_context().run, tidy_then_run)
    finally:
        give_back_idle_thread(thread)


# ---------------------------------------------------------------- the pool

# The thread of the djox.atomic() block this code runs in, or `None`.
transaction_thread: ContextVar[ThreadPoolExecutor | None] = ContextVar("djox_transaction_thread", default=None)

# Threads with no work, ready for the next call. Each keeps its own Django
# connections open. The lock is like `threading.Lock` in any Python code:
# several event loops (in several threads) may use the pool.
idle_threads: list[ThreadPoolExecutor] = []
idle_threads_lock = threading.Lock()


def take_idle_thread() -> ThreadPoolExecutor:
    """An idle thread from the pool, or a new one."""
    with idle_threads_lock:
        if idle_threads:
            return idle_threads.pop()
    # `max_workers=1`: an executor with exactly one thread, so a whole
    # transaction runs on one thread and one connection.
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="djox")


def give_back_idle_thread(thread: ThreadPoolExecutor) -> None:
    """Put a thread back in the pool, or close it if the pool is full."""
    with idle_threads_lock:
        if len(idle_threads) < MAX_IDLE_THREADS:
            idle_threads.append(thread)
            return
    # Close the thread's Django connections on the thread itself (they belong to it), then the thread.
    thread.submit(django_connections.close_all)
    thread.shutdown(wait=False)


def reading_slots_of_this_loop() -> asyncio.Semaphore:
    """The semaphore that limits how many pooled threads read at once.

    An asyncio object belongs to one event loop, and a program may run several
    loops (one per thread), so each loop has its own.
    """
    loop = asyncio.get_running_loop()
    with idle_threads_lock:
        if loop not in reading_slots:
            reading_slots[loop] = asyncio.Semaphore(MAX_READING_THREADS)
        return reading_slots[loop]


reading_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


# ---------------------------------------------------------------- connection upkeep

# How many requests have started or finished in this process. A pooled
# thread compares it with the number it saw last, to know a request went by.
requests_seen = 0
requests_seen_lock = threading.Lock()

# Each pooled thread's own memory of `requests_seen`. A `threading.local()`
# holds a separate value for each thread, like a dict keyed by thread.
this_thread = threading.local()


def note_request(**kwargs: Any) -> None:
    """Count a request starting or finishing (receiver of Django's request signals)."""
    global requests_seen  # `global`: this function changes the module's variable
    with requests_seen_lock:
        requests_seen += 1


# `dispatch_uid` makes connecting twice (the module imported twice) harmless.
request_started.connect(note_request, dispatch_uid="djox_note_request_started")
request_finished.connect(note_request, dispatch_uid="djox_note_request_finished")


def tidy_connections() -> None:
    """On a pooled thread, before its next call: if a request went by since
    the last time, run Django's `close_old_connections()` on this thread's
    connections, as Django does on a request thread (see the module docs)."""
    if getattr(this_thread, "requests_seen", None) == requests_seen:
        return
    this_thread.requests_seen = requests_seen
    close_old_connections()
