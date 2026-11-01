"""`djox.atomic()` and `djox.on_commit()`: Django's transactions, for async code.

```python
import djox

async with djox.atomic():
    author = await Author.objects.acreate(name="Frank Herbert")
    await Book.objects.acreate(title="Dune", author=author)
# Both rows are saved, or neither is.
```

How it works. A transaction belongs to one connection: every statement of the
transaction must run on it, and nothing else may run on it meanwhile. Django
keeps one connection per thread, so a sync `atomic()` block is simply "this
thread's connection, between `BEGIN` and `COMMIT`".

`djox.atomic()` gives the async code that one thread:

1. The outermost block takes a thread of its own from a small pool (a pooled
   thread keeps its Django connection open, so the next block doesn't pay
   to open and prepare one).
2. It runs Django's own `transaction.atomic()` on that thread, so Django
   starts the transaction, and later commits it or rolls it back, exactly as
   it does in sync code. Nested blocks run there too and become savepoints.
3. While the block lasts, the code inside it is tied to that thread:
   - `djox.Manager` queries run on the thread's connection (Django's
     `DatabaseWrapper` notes the open transaction, see `djox._running`);
   - Django's own async methods (`aget_or_create()`...) and your own
     `sync_to_async()` calls run on the thread itself. asgiref's
     `ThreadSensitiveContext` sends them there, instead of to Django's one
     shared "sync thread".

So everything inside the block sees the block's writes, and its writes are
committed or rolled back together.

SQLite runs one write transaction at a time, so blocks on the same file take
turns: a block waits for the previous one in asyncio, without blocking the
event loop (see `wait_for_write_turn()`).

Inside a sync transaction already (Django's `TestCase`, which runs each async
test with `async_to_sync()` inside `atomic()`), `djox.atomic()` joins it, as
a savepoint on the transaction's own thread.

Tasks started inside a block (`asyncio.gather()`) share its transaction. They
must finish before the block ends, and only one of them at a time should open
a nested block (a savepoint is a stack: two at once would mix).
"""

import asyncio
import functools
import threading
import weakref
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from contextvars import ContextVar
from types import TracebackType
from typing import Any, ParamSpec, TypeVar, overload

from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.db import DEFAULT_DB_ALIAS, OperationalError, transaction
from django.db import connections as django_connections

from djox import _running
from djox.backends.sqlite3.base import DatabaseWrapper

Parameters = ParamSpec("Parameters")
Result = TypeVar("Result")

# Idle threads kept for the next transaction. More can run at once; the
# extra ones are closed when their transaction ends. (SQLite runs one write
# transaction at a time anyway, so a few threads are plenty.)
MAX_IDLE_THREADS = 8


class Atomic:
    """One `djox.atomic()` block: `async with` it, or decorate an `async def` with it.

    It has the same arguments as Django's `transaction.atomic()`:

    - `using`: the database alias (`"default"` when `None`);
    - `savepoint`: `False` makes a nested block share its parent's fate
      (no savepoint);
    - `durable`: `True` checks the block is not nested in another one, so
      its commit really is final.
    """

    def __init__(self, using: str | None = None, savepoint: bool = True, durable: bool = False) -> None:
        self.using = using
        self.savepoint = savepoint
        self.durable = durable
        # Set while the block is open (between `__aenter__` and `__aexit__`):
        self.django_atomic: Any = None
        self.thread: ThreadPoolExecutor | None = None
        self.write_turn: asyncio.Lock | None = None
        # Leaves the `ThreadSensitiveContext` when the block ends. An
        # `AsyncExitStack` is an `async with` block that can be entered in
        # one method and left in another.
        self.thread_context = AsyncExitStack()

    async def __aenter__(self) -> None:
        """Start the transaction (or, in a nested block, a savepoint)."""
        if self.django_atomic is not None:
            raise RuntimeError("this djox.atomic() block is already open; make a new one for each block")
        alias = self.using or DEFAULT_DB_ALIAS
        sync_transaction = _running.open_sync_transaction(alias)
        self.django_atomic = transaction.atomic(using=self.using, savepoint=self.savepoint, durable=self.durable)

        def enter() -> None:
            # This runs on the transaction's thread: Django's `atomic()` uses that thread's connection.
            if sync_transaction is not None and django_connections[alias] is not sync_transaction:
                raise transaction.TransactionManagementError(
                    "djox.atomic() can't join the sync transaction around this code: it is open on another "
                    "thread. Run the async code with async_to_sync() from inside the transaction "
                    "(Django's TestCase does), or use a test with transaction=True."
                )
            self.django_atomic.__enter__()

        try:
            if transaction_thread.get() is None and sync_transaction is None:
                await self.start_outermost_block(alias)
            # `thread_sensitive=True`: run on the thread asgiref picks for this
            # context, which is the transaction's thread (see the top of this file).
            await sync_to_async(enter, thread_sensitive=True)()
        except BaseException:
            self.django_atomic = None
            await self.end_outermost_block()
            raise

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Commit, or roll back if the block raised. Like Django, the exception isn't swallowed."""
        django_atomic = self.django_atomic
        self.django_atomic = None
        try:
            await sync_to_async(django_atomic.__exit__, thread_sensitive=True)(exception_type, exception, traceback)
        finally:
            await self.end_outermost_block()

    async def start_outermost_block(self, alias: str) -> None:
        """Wait for this block's turn to write, then give it a thread of its
        own and send this task's thread-sensitive work to that thread."""
        self.write_turn = await wait_for_write_turn(alias)
        thread = take_idle_thread()
        await self.thread_context.enter_async_context(ThreadSensitiveContext(executor=thread))
        self.thread = thread
        self.transaction_thread_token = transaction_thread.set(self.thread)

    async def end_outermost_block(self) -> None:
        """Undo `start_outermost_block()`, if this block is the outermost one."""
        if self.thread is not None:
            transaction_thread.reset(self.transaction_thread_token)
            await self.thread_context.aclose()
            give_back_idle_thread(self.thread)
            self.thread = None
        if self.write_turn is not None:
            self.write_turn.release()
            self.write_turn = None

    def __call__(self, function: Callable[Parameters, Awaitable[Result]]) -> Callable[Parameters, Awaitable[Result]]:
        """Use the block as a decorator: each call of `function` runs in a new block."""

        # `functools.wraps` copies the name and docstring of `function`, like in any decorator.
        @functools.wraps(function)
        async def run_in_block(*args: Parameters.args, **kwargs: Parameters.kwargs) -> Result:
            # A new `Atomic` for each call: calls running at once need their own blocks.
            async with Atomic(self.using, self.savepoint, self.durable):
                return await function(*args, **kwargs)

        return run_in_block


# `@overload` only tells type checkers the two ways to call `atomic()`
# (`@djox.atomic` and `djox.atomic(using=...)`); the last definition is the real one.
@overload
def atomic(using: Callable[Parameters, Awaitable[Result]]) -> Callable[Parameters, Awaitable[Result]]: ...
@overload
def atomic(using: str | None = None, savepoint: bool = True, durable: bool = False) -> Atomic: ...
def atomic(
    using: Callable[Parameters, Awaitable[Result]] | str | None = None, savepoint: bool = True, durable: bool = False
) -> Atomic | Callable[Parameters, Awaitable[Result]]:
    """An async transaction block, like Django's `transaction.atomic()`.

    `async with djox.atomic():`, `@djox.atomic` or `@djox.atomic(using="other")`.
    If the block raises, the transaction is rolled back and the exception
    goes on; otherwise it is committed.
    """
    if callable(using):
        # Used as `@djox.atomic`, without parentheses: `using` is the decorated function.
        return Atomic()(using)
    return Atomic(using, savepoint, durable)


async def on_commit(function: Callable[[], object], using: str | None = None, robust: bool = False) -> None:
    """Run `function` once the current transaction commits, like Django's `transaction.on_commit()`.

    Outside a transaction it runs right away. If the transaction rolls back,
    it never runs. `function` is a plain (sync) function; it runs on the
    transaction's thread, right after the commit. `robust=True` logs its
    exceptions instead of raising them.
    """
    await sync_to_async(transaction.on_commit, thread_sensitive=True)(function, using, robust)


async def wait_for_write_turn(alias: str) -> asyncio.Lock | None:
    """Wait until no other `djox.atomic()` block of this event loop writes to
    `alias`'s SQLite file, and return the lock to release when done (or `None`
    if blocks needn't wait).

    SQLite runs one write transaction at a time. djox's `ENGINE` starts
    transactions with `BEGIN IMMEDIATE`, which takes the write lock at once,
    so two blocks can't both be open. Without this turn, the second block's
    `BEGIN` would wait inside SQLite, which retries with growing sleeps (up to
    100 ms each): with many blocks at once, most of the time would be spent
    asleep. Waiting here instead, each block starts the moment the previous
    one ends.

    Like SQLite, it gives up after the alias's `timeout` option, with
    "database is locked".
    """
    wrapper = django_connections[alias]
    if not isinstance(wrapper, DatabaseWrapper):
        return None  # not djox's SQLite: the database itself decides who waits
    options = wrapper.options()
    if options.transaction_mode not in ("IMMEDIATE", "EXCLUSIVE"):
        return None  # a deferred transaction takes the write lock only when it writes
    lock = write_turns_of_this_loop().setdefault(alias, asyncio.Lock())
    try:
        async with asyncio.timeout(options.timeout):
            await lock.acquire()
    except TimeoutError:
        raise OperationalError(
            f"database is locked: another djox.atomic() block on {alias!r} lasted more than "
            f"{options.timeout} seconds (the 'timeout' option)"
        ) from None
    return lock


def write_turns_of_this_loop() -> dict[str, asyncio.Lock]:
    """The write-turn locks of the running event loop, by alias.

    An `asyncio.Lock` belongs to one event loop, and a program may run
    several loops (one per thread). A `WeakKeyDictionary` forgets a loop's
    locks once the loop is gone.
    """
    loop = asyncio.get_running_loop()
    with write_turns_lock:
        if loop not in write_turns:
            write_turns[loop] = {}
        return write_turns[loop]


write_turns: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)
write_turns_lock = threading.Lock()

# The thread of the djox.atomic() block this code runs in, or `None`.
transaction_thread: ContextVar[ThreadPoolExecutor | None] = ContextVar("djox_transaction_thread", default=None)

# Threads with no transaction, ready for the next one. Each keeps its own
# Django connections open. The lock is like `threading.Lock` in any Python
# code: several event loops (in several threads) may use the pool.
idle_threads: list[ThreadPoolExecutor] = []
idle_threads_lock = threading.Lock()


def take_idle_thread() -> ThreadPoolExecutor:
    """An idle thread from the pool, or a new one."""
    with idle_threads_lock:
        if idle_threads:
            return idle_threads.pop()
    # `max_workers=1`: an executor with exactly one thread, so the whole
    # transaction runs on one thread and one connection.
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="djox-atomic")


def give_back_idle_thread(thread: ThreadPoolExecutor) -> None:
    """Put a thread back in the pool, or close it if the pool is full."""
    with idle_threads_lock:
        if len(idle_threads) < MAX_IDLE_THREADS:
            idle_threads.append(thread)
            return
    # Close the thread's Django connections on the thread itself (they belong to it), then the thread.
    thread.submit(django_connections.close_all)
    thread.shutdown(wait=False)
