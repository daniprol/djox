"""`djox.atomic()` and `djox.on_commit()`: Django's transactions, for async code.

```python
import djox

async with djox.atomic():
    author = await Author.objects.acreate(name="Frank Herbert")
    await Book.objects.acreate(title="Dune", author=author)
# Both rows are saved, or neither is.
```

How it works. A transaction belongs to one connection: every statement of the
transaction must run on it, and nothing else may run on it meanwhile.

On SQLite (with djox's `ENGINE` and the default `transaction_mode`,
`IMMEDIATE`), the outermost block runs its transaction on djox's own writer
connection, mostly right on the event loop's thread: see
`djox._writer_transactions`. Django joins in only if code inside the block
needs it.

Elsewhere (Postgres, or SQLite with `DEFERRED` transactions), the block
runs Django's own transaction on a thread of its own. Django keeps one
connection per thread, so a sync `atomic()` block is simply "this thread's
connection, between `BEGIN` and `COMMIT`". `djox.atomic()` gives the async
code that one thread:

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

On SQLite, either way, a block waits in asyncio until it has the file's
write turn to itself: no other block, and no plain write on its way to
djox's writer (see `djox._write_turns`).

Inside a sync transaction already (Django's `TestCase`, which runs each async
test with `async_to_sync()` inside `atomic()`), `djox.atomic()` joins it, as
a savepoint on the transaction's own thread.

Tasks started inside a block (`asyncio.gather()`) share its transaction. They
must finish before the block ends, and only one of them at a time should open
a nested block (a savepoint is a stack: two at once would mix).
"""

import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from contextvars import ContextVar
from types import TracebackType
from typing import Any, ParamSpec, TypeVar, overload

from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.db import DEFAULT_DB_ALIAS, transaction
from django.db import connections as django_connections

from djox import _running, _write_turns, _writer_transactions
from djox._threads import (
    give_back_idle_thread,
    slots_of_this_loop,
    take_idle_thread,
    tidy_connections,
    transaction_thread,
)

logger = logging.getLogger("djox")

# An `async def` passed to `on_commit()`, and whether to log its errors (`robust`).
AsyncCallback = tuple[Callable[[], Awaitable[object]], bool]

# The async callbacks of the open outermost block, run once it has ended.
# The list is shared by the block's tasks, which is why a ContextVar holds it:
# each task sees the same list object.
async_callbacks: ContextVar[list[AsyncCallback] | None] = ContextVar("djox_async_callbacks", default=None)

Parameters = ParamSpec("Parameters")
Result = TypeVar("Result")


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
        # What the outermost block set up (the write turn, its thread...), to
        # undo when it ends. An `AsyncExitStack` is a stack of `async with`
        # blocks that can be entered in one method and left in another.
        self.outermost_block = AsyncExitStack()
        # The outermost block's transaction on djox's writer connection, if it runs there.
        self.writer_transaction: _writer_transactions.WriterTransaction | None = None
        # The async `on_commit()` callbacks of the outermost block (`None` in a nested one).
        self.callbacks_after_commit: list[AsyncCallback] | None = None

    async def __aenter__(self) -> None:
        """Start the transaction (or, in a nested block, a savepoint)."""
        if self.django_atomic is not None:
            raise RuntimeError("this djox.atomic() block is already open; make a new one for each block")
        alias = self.using or DEFAULT_DB_ALIAS
        sync_transaction = _running.open_sync_transaction(alias)
        self.django_atomic = transaction.atomic(using=self.using, savepoint=self.savepoint, durable=self.durable)
        is_outermost = transaction_thread.get() is None and sync_transaction is None

        def enter() -> None:
            # This runs on the transaction's thread: Django's `atomic()` uses that thread's connection.
            if sync_transaction is not None and django_connections[alias] is not sync_transaction:
                raise transaction.TransactionManagementError(
                    "djox.atomic() can't join the sync transaction around this code: it is open on another "
                    "thread. Run the async code with async_to_sync() from inside the transaction "
                    "(Django's TestCase does), or use a test with transaction=True."
                )
            if is_outermost:
                tidy_connections()  # a pooled thread: Django's connection upkeep first
            self.django_atomic.__enter__()

        if is_outermost and _writer_transactions.runs_on_the_writer(alias):
            try:
                await self.start_on_the_writer(alias)
            except BaseException:
                self.django_atomic = None
                self.writer_transaction = None
                await self.end_outermost_block()
                raise
            return

        try:
            if is_outermost:
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
        writer_transaction = self.writer_transaction
        self.writer_transaction = None
        try:
            if writer_transaction is not None:
                await writer_transaction.end(exception_type, exception, traceback)
            else:
                await sync_to_async(django_atomic.__exit__, thread_sensitive=True)(exception_type, exception, traceback)
        finally:
            await self.end_outermost_block()
        # After the block's thread and write turn are given back: a callback
        # that writes would otherwise wait for a turn this block still holds.
        if self.callbacks_after_commit is not None:
            await run_async_callbacks(self.callbacks_after_commit)

    async def start_on_the_writer(self, alias: str) -> None:
        """Start the outermost block on djox's writer connection (see
        `djox._writer_transactions`): wait for the write turn, send this
        task's thread-sensitive work to the block's thread, and `BEGIN`.

        Each step goes on `outermost_block`, which undoes them in reverse order.
        """
        steps = self.outermost_block
        await steps.enter_async_context(_write_turns.transaction_alone(alias))
        writer_transaction = _writer_transactions.WriterTransaction(alias, self.savepoint, self.durable)
        # Closing a connection on the writer rolls back what is still open
        # (the block failed before its end), and lets the writer go on.
        steps.callback(writer_transaction.connection.close)
        thread = _writer_transactions.BlockThread(writer_transaction)
        steps.callback(thread.give_back)
        await steps.enter_async_context(ThreadSensitiveContext(executor=thread))
        thread_token = transaction_thread.set(thread)
        steps.callback(transaction_thread.reset, thread_token)
        block_token = _running.note_writer_transaction(alias, writer_transaction)
        steps.callback(_running.writer_transactions.reset, block_token)
        self.collect_async_callbacks()
        await writer_transaction.begin()
        self.writer_transaction = writer_transaction

    def collect_async_callbacks(self) -> None:
        """Start the list of async `on_commit()` callbacks the block's tasks add to."""
        self.callbacks_after_commit = []
        token = async_callbacks.set(self.callbacks_after_commit)
        self.outermost_block.callback(async_callbacks.reset, token)

    async def start_outermost_block(self, alias: str) -> None:
        """Wait for the write turn, then give the block a pooled thread of its
        own and send this task's thread-sensitive work to that thread.

        Each step goes on `outermost_block`, which undoes them in reverse order.
        """
        steps = self.outermost_block
        await steps.enter_async_context(_write_turns.transaction_alone(alias))
        await steps.enter_async_context(slots_of_this_loop(writing=True))
        thread = take_idle_thread()
        steps.callback(give_back_idle_thread, thread)
        await steps.enter_async_context(ThreadSensitiveContext(executor=thread))
        token = transaction_thread.set(thread)
        steps.callback(transaction_thread.reset, token)
        self.collect_async_callbacks()

    async def end_outermost_block(self) -> None:
        """Undo `start_outermost_block()` (nothing, for a nested block)."""
        await self.outermost_block.aclose()

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


async def on_commit(
    function: Callable[[], object] | Callable[[], Awaitable[object]], using: str | None = None, robust: bool = False
) -> None:
    """Run `function` once the current transaction commits, like Django's `transaction.on_commit()`.

    Outside a transaction it runs right away. If the transaction rolls back,
    it never runs. `function` is a plain function or an `async def`. A plain
    function runs on the transaction's thread, right after the commit. An
    `async def` is awaited on the event loop once the block has ended, so it
    can use the database freely. `robust=True` logs exceptions instead of
    raising them.
    """
    if not inspect.iscoroutinefunction(function):
        await sync_to_async(transaction.on_commit, thread_sensitive=True)(function, using, robust)
        return
    # Django only calls plain functions. Ours just notes the async function in
    # the list of the open block (or in a list of its own, outside a block,
    # where Django calls it at once); the list is awaited after the commit.
    callbacks = async_callbacks.get()
    in_a_block = callbacks is not None
    if callbacks is None:
        callbacks = []

    def note_it() -> None:
        callbacks.append((function, robust))

    await sync_to_async(transaction.on_commit, thread_sensitive=True)(note_it, using)
    if not in_a_block:
        await run_async_callbacks(callbacks)


async def run_async_callbacks(callbacks: list[AsyncCallback]) -> None:
    """Await the async `on_commit()` callbacks in order, like Django runs the sync ones."""
    for function, robust in callbacks:
        try:
            await function()
        except Exception:
            if not robust:
                raise
            logger.exception("Error calling %r in on_commit() (robust call).", function)
