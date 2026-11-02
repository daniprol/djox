"""Where a query of `djox.QuerySet` runs: on djox's async database, in an
open sync transaction, or, for sync code, on Django's own connection.

`djox.QuerySet` builds SQL with Django's compiler, then calls `fetch_all()`,
`stream_rows()` or `execute()` here to run it (from sync code,
`fetch_all_now()` and `execute_now()`). Usually the SQL goes to the async `djox.Database` of the
alias (`djox.connections[alias]`), which never blocks the event loop.

There is one exception. When this code runs inside a sync transaction on the
same alias, the SQL must run inside that transaction. A transaction
only shows its changes to its own connection until it commits, so another
connection wouldn't see them, and a write from another connection would wait
for the transaction's lock (and the transaction may be waiting for us). The
classic case is Django's `TestCase`: each test runs inside a sync `atomic()`
block, and an async test must see the rows its `setUpTestData()` wrote.

In that case the SQL runs on the transaction's own connection (a `djox.Connection`
on SQLite, psycopg's on Postgres), on a
worker thread (`asyncio.to_thread()`), so the event loop still doesn't block.

Both ways do what Django's cursors do: turn djox's exceptions into Django's
(`django.db.IntegrityError`...), and log the query when Django logs queries
(`DEBUG = True`, or inside `assertNumQueries()` and `CaptureQueriesContext`).
"""

import asyncio
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from django.db import connections as django_connections

import djox
from djox import hooks
from djox.backends.engine import DjoxEngine

if TYPE_CHECKING:
    # `Row` exists only in the type hints of `djox._core` (`_core.pyi`).
    from djox._core import Row


async def fetch_all(alias: str, sql: str, parameters: Sequence[Any], writes: bool = False) -> "list[Row]":
    """Run a statement and return all its rows. `sql` uses Django's `%s` placeholders.

    `writes` is `True` for a statement that writes and returns rows (`INSERT
    ... RETURNING`): it then takes part in the write turn, like `execute()`.
    """
    if hooks.added_hooks:
        with hooks.around_query(alias, sql, parameters) as query:
            return await fetch_all_without_hooks(alias, query.sql, query.parameters, writes)
    return await fetch_all_without_hooks(alias, sql, parameters, writes)


async def fetch_all_without_hooks(alias: str, sql: str, parameters: Sequence[Any], writes: bool) -> "list[Row]":
    """`fetch_all()`, once the hooks (`djox.hooks`) have seen the statement."""
    started = time.perf_counter()
    transaction_wrapper = open_sync_transaction(alias)
    if transaction_wrapper is not None:
        # `with` around `await` is fine: the block simply lasts until the await is done.
        with transaction_wrapper.wrap_database_errors:
            rows = await asyncio.to_thread(fetch_all_on, transaction_wrapper.connection, sql, parameters)
        log_query(transaction_wrapper, sql, parameters, started)
    else:
        wrapper = django_connections[alias]
        database = djox.connections.database_for(wrapper)
        with wrapper.wrap_database_errors:
            if writes:
                async with write_turns().plain_write(alias):
                    rows = await database.fetch_all(sql, parameters)
            else:
                rows = await database.fetch_all(sql, parameters)
        rows = as_sync_connections_return_them(wrapper, rows)
        log_query(wrapper, sql, parameters, started)
    return rows


async def stream_rows(alias: str, sql: str, parameters: Sequence[Any], chunk_size: int) -> "AsyncIterator[list[Row]]":
    """Run a query and yield its rows `chunk_size` at a time, each chunk read
    from the database when the previous one has been used (`aiterator()`).

    The rows come from a connection of the stream's own (`Database.stream_rows()`).
    The hooks and Django's query log see the statement once, when its first
    chunk arrives: that is when Django's cursor would have run it. If the
    caller stops early, the rest of the rows are never read.
    """
    if open_sync_transaction(alias) is not None:
        # Only the transaction's connection sees its writes, and it runs one
        # statement at a time: read every row there, then hand them out.
        rows = await fetch_all(alias, sql, parameters)
        for start in range(0, len(rows), chunk_size):
            yield rows[start : start + chunk_size]
        return
    wrapper = django_connections[alias]
    query = hooks.run_before_query(hooks.Query(alias, sql, parameters))
    started = time.perf_counter()
    try:
        with wrapper.wrap_database_errors:
            database = djox.connections.database_for(wrapper)
            stream = database.stream_rows(query.sql, query.parameters, chunk_size=chunk_size)
            rows = await stream.next_chunk()
    except BaseException as error:
        hooks.run_after_query(query, hooks.QueryFinished(time.perf_counter() - started, error))
        raise
    hooks.run_after_query(query, hooks.QueryFinished(time.perf_counter() - started))
    log_query(wrapper, query.sql, query.parameters, started)
    # A chunk shorter than `chunk_size` is the last one.
    while len(rows) == chunk_size:
        yield as_sync_connections_return_them(wrapper, rows)
        with wrapper.wrap_database_errors:
            rows = await stream.next_chunk()
    if rows:
        yield as_sync_connections_return_them(wrapper, rows)


def as_sync_connections_return_them(wrapper: Any, rows: "list[Row]") -> "list[Row]":
    """The async database's rows, as this alias's sync connections would
    return them (on Postgres, dates and times in the connection's time zone)."""
    # Every alias djox runs has a djox `ENGINE`, whose `DatabaseWrapper` is a `DjoxEngine`.
    engine: DjoxEngine = wrapper
    return engine.rows_from_async_database(rows)


def fetch_all_now(alias: str, sql: str, parameters: Sequence[Any]) -> "list[Row]":
    """Run a statement from sync code and return all its rows.

    Like Django's sync methods: on this thread's Django connection, through
    a Django cursor, so it joins this thread's transaction, is logged, and
    raises Django's exceptions, all by Django's own code. (And, like them,
    it refuses to run inside async code: `SynchronousOnlyOperation`.)
    """
    # (Django's cursor runs the hooks, `djox.hooks.execute_with_hooks()`.)
    with django_connections[alias].cursor() as cursor:
        cursor.execute(sql, parameters)
        return cursor.fetchall()  # type: ignore[no-any-return]  # Django's cursor is untyped


def execute_now(alias: str, sql: str, parameters: Sequence[Any]) -> int:
    """Run a statement that changes data from sync code, like `fetch_all_now()`,
    and return how many rows it changed."""
    with django_connections[alias].cursor() as cursor:
        cursor.execute(sql, parameters)
        return cursor.rowcount  # type: ignore[no-any-return]  # Django's cursor is untyped


async def execute(alias: str, sql: str, parameters: Sequence[Any]) -> int:
    """Run a statement that changes data and return how many rows it changed."""
    if hooks.added_hooks:
        with hooks.around_query(alias, sql, parameters) as query:
            return await execute_without_hooks(alias, query.sql, query.parameters)
    return await execute_without_hooks(alias, sql, parameters)


async def execute_without_hooks(alias: str, sql: str, parameters: Sequence[Any]) -> int:
    """`execute()`, once the hooks (`djox.hooks`) have seen the statement."""
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
                executed = await djox.connections.database_for(wrapper).execute(sql, parameters)
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
    # The note may be stale: the connection was closed, or the transaction
    # ended. (`autocommit` is Django's own record of `set_autocommit()`.)
    if connection is None or connection.closed or not (wrapper.in_atomic_block or not wrapper.autocommit):
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


def fetch_all_on(connection: Any, sql: str, parameters: Sequence[Any]) -> "list[Row]":
    """Run a statement on a sync connection and return its rows (on a worker thread).

    `connection` is the driver's connection under Django's `DatabaseWrapper`:
    a `djox.Connection` on SQLite, a psycopg connection on Postgres. Both
    have the same `cursor()`, `execute()` and `fetchall()` (PEP 249).
    """
    cursor = connection.cursor()
    try:
        cursor.execute(sql, parameters)
        return cursor.fetchall()  # type: ignore[no-any-return]  # the driver's cursor is untyped here
    finally:
        cursor.close()


def execute_on(connection: Any, sql: str, parameters: Sequence[Any]) -> int:
    """Run a statement on a sync connection and return how many rows it changed."""
    cursor = connection.cursor()
    try:
        cursor.execute(sql, parameters)
        return cursor.rowcount  # type: ignore[no-any-return]  # the driver's cursor is untyped here
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
