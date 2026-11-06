"""Transactions on djox's writer connection: how `djox.atomic()` runs on SQLite.

SQLite lets one connection write at a time, and djox's async database
already has one writer connection (`djox.connections[alias]`). So an
outermost `djox.atomic()` block on SQLite runs its transaction right there,
on a `djox.Connection` from `Database.writer_connection()`:

- `BEGIN`, the block's statements and `COMMIT` run on the event loop's
  thread when they need no waiting (`Cursor.execute_quickly()`). A small
  `INSERT` takes about 20 µs there; a trip to another thread and back takes
  80 to 100 µs. A statement that may take longer (a long read, a write not
  yet seen to be quick, a `BEGIN` that must wait for another process) runs on
  another thread instead (`asyncio.to_thread()`), so the event loop never
  waits. `crates/djox/src/sqlite/writer.rs` has the rules.
- Until the block ends, no other statement runs on the writer connection:
  djox's writer thread waits, and this event loop's plain writes wait for
  their write turn (`djox._write_turns`).

Django isn't involved until it is needed. Some code inside a block runs
Django's sync code: Django's own write methods (`aget_or_create()`,
`abulk_create()`...), a nested `djox.atomic()` (a savepoint),
`djox.on_commit()`, your own `sync_to_async()` calls. asgiref sends all of
it to the block's thread (`BlockThread`). The first time something runs
there, the thread *joins* the transaction: its Django connection is pointed
at the writer connection, and Django's own `atomic()` starts on it, without
a `BEGIN` of its own. From then on Django runs the block as it does in sync
code, and ends it: the commit (or rollback), and the on-commit callbacks.
"""

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeVar

from asgiref.sync import sync_to_async
from django.db import connections as django_connections
from django.db import transaction

import djox
from djox._threads import give_back_idle_thread, take_idle_thread, tidy_connections

if TYPE_CHECKING:
    # `Row` exists only in the type hints of `djox._core` (`_core.pyi`).
    from djox._core import Row

Result = TypeVar("Result")


def runs_on_the_writer(alias: str) -> bool:
    """Whether an outermost `djox.atomic()` block on `alias` runs on djox's
    writer connection: SQLite with djox's `ENGINE`, and transactions that
    take the write lock at `BEGIN` (`transaction_mode`, `IMMEDIATE` by default).

    With `DEFERRED` (or `None`), a transaction only takes the lock when it
    first writes, and may never write at all; it then runs on a Django
    connection of its own, as on Postgres (see `djox.transaction`).
    """
    # Imported here: djox's `DatabaseWrapper` module imports `djox._running`,
    # which this module imports too (Python can't import two modules that
    # need each other at the top).
    from djox.backends.sqlite3.base import DatabaseWrapper

    wrapper = django_connections[alias]
    if not isinstance(wrapper, DatabaseWrapper):
        return False
    return wrapper.options().transaction_mode in ("IMMEDIATE", "EXCLUSIVE")


class WriterTransaction:
    """The transaction of one outermost `djox.atomic()` block, on djox's
    writer connection (see the module docs).

    `djox.transaction.Atomic` makes it, calls `begin()` when the block starts
    and `end()` when it ends. In between, `djox._running` sends the block's
    statements to `fetch_all()` and `execute()`.
    """

    def __init__(self, alias: str, savepoint: bool, durable: bool) -> None:
        self.alias = alias
        # The arguments of the `djox.atomic()` block, for Django's `atomic()` if the thread joins.
        self.savepoint = savepoint
        self.durable = durable
        # `Any`: django-stubs only knows Django's own `DatabaseWrapper`, not djox's.
        wrapper: Any = django_connections[alias]
        self.transaction_mode: str = wrapper.options().transaction_mode
        self.connection = djox.connections.database_for(wrapper).writer_connection()
        # Set when a statement of the block failed in a way that forbids
        # committing (like Django's `needs_rollback`), with the error.
        self.needs_rollback = False
        self.rollback_error: BaseException | None = None
        # Once the block's thread joined (see `join_on_this_thread()`):
        # Django's `atomic()` block, the thread's `DatabaseWrapper`, and the
        # connection (and autocommit record) it had before, to give back at the end.
        self.django_atomic: Any = None
        self.django_wrapper: Any = None
        self.connection_before: Any = None

    async def begin(self) -> None:
        """Start the transaction: `BEGIN IMMEDIATE` (or the alias's `transaction_mode`)."""
        await self.run(f"BEGIN {self.transaction_mode}", [])

    async def end(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Commit, or roll back if the block raised or must roll back. If the
        block's thread joined, Django's `atomic()` does it, on that thread.

        Like Django, a failed commit (a deferred foreign key check) rolls back
        and raises. The connection is closed by `djox.transaction.Atomic`
        afterwards, which rolls back anything still open.
        """
        if self.django_atomic is not None:
            await sync_to_async(self.end_on_this_thread, thread_sensitive=True)(exception_type, exception, traceback)
        elif exception_type is None and not self.needs_rollback:
            try:
                await self.run("COMMIT", [])
            except BaseException:
                await self.run("ROLLBACK", [])
                raise
        else:
            await self.run("ROLLBACK", [])

    async def fetch_all(self, sql: str, parameters: Sequence[Any]) -> "list[Row]":
        """Run a statement of the block and return its rows."""
        self.check_can_run_queries()
        cursor = await self.run(sql, parameters)
        return cursor.fetchall()

    async def fetch_all_with_columns(self, sql: str, parameters: Sequence[Any]) -> "tuple[list[str], list[Row]]":
        """Like `fetch_all()`, with the column names too."""
        self.check_can_run_queries()
        cursor = await self.run(sql, parameters)
        columns = [column[0] for column in cursor.description or ()]
        return columns, cursor.fetchall()

    async def execute(self, sql: str, parameters: Sequence[Any]) -> int:
        """Run a statement of the block that changes data, and return how many rows it changed."""
        self.check_can_run_queries()
        cursor = await self.run(sql, parameters)
        return cursor.rowcount

    async def run(self, sql: str, parameters: Sequence[Any]) -> djox.Cursor:
        """Run a statement on the writer connection: right here if it needs
        no waiting, otherwise on another thread. `sql` uses Django's `%s`
        placeholders (so `parameters` is a list, even an empty one)."""
        cursor = self.connection.cursor()
        # Django's exceptions (`django.db.IntegrityError`...), like its cursors raise.
        with django_connections[self.alias].wrap_database_errors:
            if cursor.execute_quickly(sql, list(parameters)):
                return cursor
            on_a_thread = asyncio.ensure_future(asyncio.to_thread(cursor.execute, sql, list(parameters)))
            try:
                # `shield`: if this task is cancelled, the statement still runs
                # to the end on its thread, and the block must not end before it.
                await asyncio.shield(on_a_thread)
            except asyncio.CancelledError:
                await asyncio.wait([on_a_thread])
                raise
        return cursor

    def check_can_run_queries(self) -> None:
        """Like Django's cursors inside `atomic()`: after an error that forbids
        committing, nothing runs until the block is rolled back."""
        if self.django_wrapper is not None:
            self.django_wrapper.validate_no_broken_transaction()
        if self.needs_rollback:
            raise transaction.TransactionManagementError(
                "An error occurred in the current transaction. You can't execute queries until the end of the "
                "'atomic' block."
            ) from self.rollback_error

    def mark_for_rollback(self, error: BaseException) -> None:
        """A write of the block failed (`djox._running.mark_for_rollback_on_error()`):
        the block must not commit half of its work."""
        self.needs_rollback = True
        self.rollback_error = error
        if self.django_wrapper is not None:
            self.django_wrapper.needs_rollback = True
            # Recent Django versions name this error when they refuse the next query.
            self.django_wrapper.rollback_exc = error

    # ------------------------------------------------------------ on the block's thread

    def join_on_this_thread(self) -> None:
        """On the block's thread, before its first call: point this thread's
        Django connection at the writer connection, and start Django's own
        `atomic()` there, which joins the transaction already open."""
        if self.django_atomic is not None:
            return
        tidy_connections()  # a pooled thread: Django's connection upkeep first
        wrapper: Any = django_connections[self.alias]  # djox's, which django-stubs doesn't know
        django_atomic = transaction.atomic(using=self.alias, savepoint=self.savepoint, durable=self.durable)
        self.connection_before = (wrapper.connection, wrapper.autocommit)
        wrapper.connection = self.connection
        # Django's record of the connection's autocommit mode, which its
        # `connect()` would set: a thread that never connected says `False`.
        wrapper.autocommit = True
        # Django's `atomic()` would run `BEGIN`: djox's `DatabaseWrapper` skips
        # it while this is set (see `_start_transaction_under_autocommit()`).
        wrapper.joins_open_transaction = True
        try:
            django_atomic.__enter__()
        except BaseException:
            wrapper.connection, wrapper.autocommit = self.connection_before
            raise
        finally:
            wrapper.joins_open_transaction = False
        self.django_atomic = django_atomic
        self.django_wrapper = wrapper
        if self.needs_rollback:
            wrapper.needs_rollback = True
            wrapper.rollback_exc = self.rollback_error

    def end_on_this_thread(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """On the block's thread: end Django's `atomic()` (it commits or rolls
        back, then runs the on-commit callbacks), and give the thread's Django
        connection back what it had before."""
        try:
            self.django_atomic.__exit__(exception_type, exception, traceback)
        finally:
            self.django_wrapper.connection, self.django_wrapper.autocommit = self.connection_before


class BlockThread(Executor):
    """The thread of a `djox.atomic()` block on the writer: where asgiref
    sends the block's sync code (`sync_to_async(..., thread_sensitive=True)`).

    It is an `Executor`, like a `ThreadPoolExecutor(max_workers=1)`: a pooled
    thread from `djox._threads`, taken the first time it gets work (most
    blocks never need it). Before running anything, it joins the block's
    transaction (`WriterTransaction.join_on_this_thread()`).
    """

    def __init__(self, writer_transaction: WriterTransaction) -> None:
        self.writer_transaction = writer_transaction
        self.thread: ThreadPoolExecutor | None = None

    def submit(self, function: Callable[..., Result], /, *args: Any, **kwargs: Any) -> "Future[Result]":
        """Run `function(*args, **kwargs)` on the thread, once it joined the transaction."""
        if self.thread is None:
            self.thread = take_idle_thread()

        def join_then_run() -> Result:
            self.writer_transaction.join_on_this_thread()
            return function(*args, **kwargs)

        return self.thread.submit(join_then_run)

    def give_back(self) -> None:
        """Put the thread back in the pool, if it was taken."""
        if self.thread is not None:
            give_back_idle_thread(self.thread)
            self.thread = None
