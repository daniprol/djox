"""Where an async query runs: on djox's async database, or in an open sync transaction.

`djox.QuerySet` builds SQL with Django's compiler, then calls `fetch_all()` or
`execute()` here to run it. Usually the SQL goes to the async `djox.Database`
of the alias (`djox.connections[alias]`), which never blocks the event loop.

There is one exception. When this code runs inside a sync transaction on the
same alias, the SQL must run inside that transaction. A transaction
only shows its changes to its own connection until it commits, so another
connection wouldn't see them, and a write from another connection would wait
for the transaction's lock (and the transaction may be waiting for us). The
classic case is Django's `TestCase`: each test runs inside a sync `atomic()`
block, and an async test must see the rows its `setUpTestData()` wrote.

In that case the SQL runs on the transaction's own `djox.Connection`, on a
worker thread (`asyncio.to_thread()`), so the event loop still doesn't block.

Both ways do what Django's cursors do: turn djox's exceptions into Django's
(`django.db.IntegrityError`...), and log the query when Django logs queries
(`DEBUG = True`, or inside `assertNumQueries()` and `CaptureQueriesContext`).
"""

import asyncio
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from django.db import connections as django_connections

import djox

if TYPE_CHECKING:
    # `Row` exists only in the type hints of `djox._core` (`_core.pyi`).
    from djox._core import Row


async def fetch_all(alias: str, sql: str, parameters: Sequence[Any], writes: bool = False) -> "list[Row]":
    """Run a statement and return all its rows. `sql` uses Django's `%s` placeholders.

    `writes` is `True` for a statement that writes and returns rows (`INSERT
    ... RETURNING`): it then takes part in the write turn, like `execute()`.
    """
    started = time.perf_counter()
    transaction_wrapper = open_sync_transaction(alias)
    if transaction_wrapper is not None:
        # `with` around `await` is fine: the block simply lasts until the await is done.
        with transaction_wrapper.wrap_database_errors:
            rows = await asyncio.to_thread(fetch_all_on, transaction_wrapper.connection, sql, parameters)
        log_query(transaction_wrapper, sql, parameters, started)
    else:
        wrapper = django_connections[alias]
        with wrapper.wrap_database_errors:
            if writes:
                async with write_turns().plain_write(alias):
                    rows = await djox.connections[alias].fetch_all(sql, parameters)
            else:
                rows = await djox.connections[alias].fetch_all(sql, parameters)
        log_query(wrapper, sql, parameters, started)
    return rows


async def execute(alias: str, sql: str, parameters: Sequence[Any]) -> int:
    """Run a statement that changes data and return how many rows it changed."""
    started = time.perf_counter()
    transaction_wrapper = open_sync_transaction(alias)
    if transaction_wrapper is not None:
        with transaction_wrapper.wrap_database_errors:
            rows_affected = await asyncio.to_thread(execute_on, transaction_wrapper.connection, sql, parameters)
        log_query(transaction_wrapper, sql, parameters, started)
    else:
        wrapper = django_connections[alias]
        with wrapper.wrap_database_errors:
            # Shares the write turn with other plain writes, and waits for a
            # transaction that has it (see `djox._write_turns`).
            async with write_turns().plain_write(alias):
                executed = await djox.connections[alias].execute(sql, parameters)
        rows_affected = executed.rows_affected
        log_query(wrapper, sql, parameters, started)
    return rows_affected


def write_turns() -> Any:
    """The `djox._write_turns` module.

    Imported here, when first needed, rather than at the top of this file:
    it needs djox's `DatabaseWrapper`, whose module imports this one, and
    Python can't import two modules that each need the other at the top.
    """
    from djox import _write_turns

    return _write_turns


def open_sync_transaction(alias: str) -> Any:
    """The sync connection (Django's `DatabaseWrapper`) of `alias` whose
    transaction this code runs in, or `None`.

    Django keeps one sync connection per thread, so async code (on the event
    loop's thread) can't simply look at "its" connection. Instead, djox's
    `DatabaseWrapper` notes each transaction it opens in `sync_transactions`,
    a context variable (see `note_sync_transaction()`). Async code started
    from inside the transaction (Django's test runner wraps each async test
    in `async_to_sync()`, which copies the context) sees the note; unrelated
    async code, like another request, has its own context and doesn't.
    """
    wrapper = noted_sync_transaction(alias)
    if wrapper is not None:
        # Like Django's cursors: after an error inside `atomic()`, nothing runs
        # until the block is rolled back (raises `TransactionManagementError`).
        wrapper.validate_no_broken_transaction()
    return wrapper


def noted_sync_transaction(alias: str) -> Any:
    """Like `open_sync_transaction()`, without checking that the transaction can still run SQL."""
    wrapper = sync_transactions.get().get(alias)
    if wrapper is None:
        return None
    connection = wrapper.connection
    # The note may be stale: the connection was closed, or the transaction ended.
    if connection is None or connection.closed or not (wrapper.in_atomic_block or connection.in_transaction):
        return None
    return wrapper


@contextmanager
def mark_for_rollback_on_error(alias: str) -> Iterator[None]:
    """If a write fails inside an `atomic()` block, mark the block for rollback.

    Like Django's `transaction.mark_for_rollback_on_error()`, which `save()`
    and `update()` use: the caller may catch the error and go on, but the
    transaction must not commit half of its work. It marks the right
    connection: the one of the transaction this code runs in.

    (`@contextmanager` turns this generator into a `with` block: the code
    inside the `with` runs at the `yield`.)
    """
    try:
        yield
    except Exception as error:
        # `Any`: django-stubs doesn't know `rollback_exc`.
        wrapper: Any = noted_sync_transaction(alias) or django_connections[alias]
        if wrapper.in_atomic_block:
            wrapper.needs_rollback = True
            # Recent Django versions name this error when they refuse the next query.
            wrapper.rollback_exc = error
        raise


# The sync connections (Django `DatabaseWrapper`s) with a transaction open in
# this context, by alias. A `ContextVar` is like a global variable, but each
# thread and asyncio task sees its own value, and a copied context (like the
# one `async_to_sync()` makes) starts with the values of the original.
sync_transactions: ContextVar[dict[str, Any]] = ContextVar("djox_sync_transactions", default={})  # noqa: B039  (never changed in place)


def note_sync_transaction(alias: str, wrapper: Any, is_open: bool) -> None:
    """Note that `wrapper` (a Django `DatabaseWrapper`) opened or closed a transaction.

    Called by djox's `DatabaseWrapper` when Django turns autocommit off
    (`atomic()` starting) or back on (`atomic()` ending). The dict is copied,
    never changed in place, because copied contexts share the same dict.
    """
    transactions = dict(sync_transactions.get())
    if is_open:
        transactions[alias] = wrapper
    else:
        transactions.pop(alias, None)
    sync_transactions.set(transactions)


def fetch_all_on(connection: djox.Connection, sql: str, parameters: Sequence[Any]) -> "list[Row]":
    """Run a statement on a sync connection and return its rows (on a worker thread)."""
    cursor = connection.cursor()
    try:
        return cursor.execute(sql, parameters).fetchall()
    finally:
        cursor.close()


def execute_on(connection: djox.Connection, sql: str, parameters: Sequence[Any]) -> int:
    """Run a statement on a sync connection and return how many rows it changed."""
    cursor = connection.cursor()
    try:
        return cursor.execute(sql, parameters).rowcount
    finally:
        cursor.close()


def log_query(wrapper: Any, sql: str, parameters: Sequence[Any], started: float) -> None:
    """Add the query to the query log of `wrapper` (a Django `DatabaseWrapper`),
    if Django is logging queries.

    It is the list behind `connection.queries`, `assertNumQueries()` and the
    Django Debug Toolbar. A query that ran in a sync transaction is logged on
    that transaction's connection.
    """
    if not wrapper.queries_logged:
        return
    seconds = time.perf_counter() - started
    wrapper.queries_log.append({"sql": sql_with_parameters(sql, parameters), "time": f"{seconds:.3f}"})


def sql_with_parameters(sql: str, parameters: Sequence[Any]) -> str:
    """The SQL with its parameters written in, for the query log only (never run)."""
    if not parameters:
        return sql
    quoted = []
    for value in parameters:
        if value is None:
            quoted.append("NULL")
        elif isinstance(value, str):
            quoted.append("'" + value.replace("'", "''") + "'")
        else:
            quoted.append(str(value))
    try:
        return sql % tuple(quoted)
    except (TypeError, ValueError):
        return f"{sql} -- parameters: {list(parameters)!r}"
