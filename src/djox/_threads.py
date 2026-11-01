"""djox's own threads for Django's sync code, and the turns writers take.

Some work can only run as Django's sync code: a transaction (`atomic()` must
run on one thread, with that thread's connection), and Django methods that
djox doesn't run itself yet (`get_or_create()`, `bulk_create()`, deletes that
cascade, `prefetch_related()`, `Model.save()`...).

Django's own async methods send all of that to one shared "sync thread", one
call at a time, for the whole process. djox sends it to a small pool of its
own threads instead, so these calls don't queue behind each other (or behind
Django's other async calls). Each thread keeps its Django connections open,
so the next call doesn't pay to open and prepare one.

| Function               | What it does                                                |
|------------------------|-------------------------------------------------------------|
| `run_django_method()`  | Run a sync Django method on a pooled thread (or in the transaction this code is in) |
| `take_idle_thread()`, `give_back_idle_thread()` | The pool itself (`djox.atomic()` uses it too) |
| `wait_for_write_turn()` | Writers on one SQLite file take turns, in asyncio            |
"""

import asyncio
import contextvars
import threading
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from typing import TypeVar

from asgiref.sync import sync_to_async
from django.db import OperationalError
from django.db import connections as django_connections

from djox import _running
from djox.backends.sqlite3.base import DatabaseWrapper

Result = TypeVar("Result")

# Idle threads kept for the next call. More can run at once; the extra ones
# are closed when their call ends.
MAX_IDLE_THREADS = 8

# The most threads that read at once, per event loop. (Writers take turns, so
# at most one writes.) Each thread holds a connection.
MAX_READING_THREADS = 8


async def run_django_method(alias: str, method: Callable[[], Result], writes: bool) -> Result:
    """Run Django's sync `method` off the event loop, and return its result.

    Inside a transaction (a `djox.atomic()` block, or a sync `atomic()` around
    this code), it runs on the transaction's thread, so it joins the
    transaction. Otherwise it runs on a pooled thread of its own. `writes`
    says whether it may write: then it waits for its turn on `alias`'s file
    first (see `wait_for_write_turn()`).

    On a database whose `ENGINE` isn't djox's, it runs like Django's own
    async methods do (on Django's sync thread).
    """
    if not isinstance(django_connections[alias], DatabaseWrapper):
        return await sync_to_async(method)()
    if transaction_thread.get() is not None or _running.open_sync_transaction(alias) is not None:
        # `thread_sensitive=True` sends it to the transaction's thread (asgiref
        # finds it from this context, like `sync_to_async()` in your own code).
        return await sync_to_async(method, thread_sensitive=True)()

    write_turn = await wait_for_write_turn(alias) if writes else None
    reading_slot = None if writes else reading_slots_of_this_loop()
    try:
        if reading_slot is not None:
            await reading_slot.acquire()
        thread = take_idle_thread()
        try:
            # `run_in_executor()` runs `method` on `thread` and lets this task
            # await it. `copy_context().run` runs it with this task's context
            # variables, like `sync_to_async()` does.
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(thread, contextvars.copy_context().run, method)
        finally:
            give_back_idle_thread(thread)
    finally:
        if reading_slot is not None:
            reading_slot.release()
        if write_turn is not None:
            write_turn.release()


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


# ---------------------------------------------------------------- turns


async def wait_for_write_turn(alias: str) -> asyncio.Lock | None:
    """Wait until nothing else of this event loop writes to `alias`'s SQLite
    file from djox's threads, and return the lock to release when done (or
    `None` if writers needn't wait).

    SQLite runs one write transaction at a time. djox's `ENGINE` starts
    transactions with `BEGIN IMMEDIATE`, which takes the write lock at once,
    so two can't both be open. Without turns, the second `BEGIN` would wait
    inside SQLite, which retries with growing sleeps (up to 100 ms each):
    with many writers at once, most of the time would be spent asleep.
    Waiting here instead, each writer starts the moment the previous one ends.

    Like SQLite, it gives up after the alias's `timeout` option, with
    "database is locked".
    """
    wrapper = django_connections[alias]
    if not isinstance(wrapper, DatabaseWrapper):
        return None  # not djox's SQLite: the database itself decides who waits
    options = wrapper.options()
    if options.transaction_mode not in ("IMMEDIATE", "EXCLUSIVE"):
        return None  # a deferred transaction takes the write lock only when it writes
    lock = locks_of_this_loop(write_turns, alias, asyncio.Lock)
    try:
        async with asyncio.timeout(options.timeout):
            await lock.acquire()
    except TimeoutError:
        raise OperationalError(
            f"database is locked: another write to {alias!r} lasted more than "
            f"{options.timeout} seconds (the 'timeout' option)"
        ) from None
    return lock


def reading_slots_of_this_loop() -> asyncio.Semaphore:
    """The semaphore that limits how many pooled threads read at once."""
    return locks_of_this_loop(reading_slots, "all", lambda: asyncio.Semaphore(MAX_READING_THREADS))


LockType = TypeVar("LockType")


def locks_of_this_loop(
    locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, LockType]]",
    name: str,
    make_lock: Callable[[], LockType],
) -> LockType:
    """The lock called `name` of the running event loop, made on first use.

    An asyncio lock belongs to one event loop, and a program may run several
    loops (one per thread). A `WeakKeyDictionary` forgets a loop's locks
    once the loop is gone.
    """
    loop = asyncio.get_running_loop()
    with locks_lock:
        if loop not in locks:
            locks[loop] = {}
        if name not in locks[loop]:
            locks[loop][name] = make_lock()
        return locks[loop][name]


write_turns: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)
reading_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]]" = (
    weakref.WeakKeyDictionary()
)
locks_lock = threading.Lock()
