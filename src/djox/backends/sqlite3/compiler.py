"""Django's SQL compilers, able to hand their statement to djox's async code.

A compiler turns a Django `Query` into SQL (`as_sql()`) and runs it
(`execute_sql()`). Django's sync methods, like `QuerySet.count()` or
`update()`, build a query, ask for its compiler, call `execute_sql()` and
then shape the answer (a number, a dict...).

`djox.QuerySet` wants Django's own code for all of that, except the one
moment when the SQL runs: that must be awaited on djox instead. So these
compilers have a second mode, used by `replay()` below (and by `capture()`
and `run_first_statement()`, which stop after step 2):

1. Run Django's sync method. When it reaches `execute_sql()`, the compiler
   stops it by raising `StatementNeeded`, which carries the compiler.
2. Compile the SQL and await it on djox (`djox._running`).
3. Run the sync method again. This time `execute_sql()` returns the answer
   from step 2 right away, and the method finishes normally.

The method runs twice, so this is only used for methods that do nothing but
build a query before running it (no signals, no defaults computed). A method
that runs two statements is replayed three times: the first answer is
reused, the second statement is awaited.

Running a method twice costs time, so when the last step after the statement
is simple (`count()` returns the first column), `run_first_statement()` stops
after step 2 and the caller finishes the method in a line or two.

Otherwise, these compilers are exactly Django's.

| Class                   | What it compiles               |
|-------------------------|--------------------------------|
| `SQLCompiler`           | `SELECT`                       |
| `SQLInsertCompiler`     | `INSERT`                       |
| `SQLUpdateCompiler`     | `UPDATE`                       |
| `SQLDeleteCompiler`     | `DELETE`                       |
| `SQLAggregateCompiler`  | `SELECT` around a subquery, for aggregates |
"""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

from django.core.exceptions import EmptyResultSet
from django.db.models.sql import compiler
from django.db.models.sql.constants import CURSOR, GET_ITERATOR_CHUNK_SIZE, MULTI, NO_RESULTS, ROW_COUNT, SINGLE

from djox import _running

Result = TypeVar("Result")


@dataclass
class Replay:
    """The answers of the statements already run, handed out in order."""

    answers: list[Any] = field(default_factory=list)
    next_answer: int = 0

    def take_answer(self, statement: "StatementNeeded") -> Any:
        """The answer for the next statement, or raise `statement` if it hasn't run yet."""
        if self.next_answer < len(self.answers):
            answer = self.answers[self.next_answer]
            self.next_answer += 1
            return answer
        raise statement


# The replay in progress in this context, or `None` (then compilers act like
# Django's). A `ContextVar` is like a global variable, but each asyncio task
# (and thread) sees its own value, so tasks running at once don't mix.
current_replay: ContextVar[Replay | None] = ContextVar("djox_current_replay", default=None)


class StatementNeeded(BaseException):
    """Raised by a compiler during a replay, when a statement must run on djox.

    It derives from `BaseException`, not `Exception`, like `GeneratorExit`:
    Django's code has `except Exception:` blocks (for example
    `transaction.mark_for_rollback_on_error()`) that must not mistake it
    for a failure.
    """

    def __init__(self, compiler: "compiler.SQLCompiler", result_type: str | None) -> None:
        super().__init__("a statement needs to run on djox")
        self.compiler = compiler
        self.result_type = result_type


async def replay(alias: str, method: Callable[[], Result]) -> Result:
    """Run Django's sync `method` with each statement it runs awaited on djox.

    See the top of this file. `method` must be safe to run more than once.
    """
    answers: list[Any] = []
    while True:
        statement = None
        token = current_replay.set(Replay(answers))
        try:
            return method()
        except StatementNeeded as needed:
            statement = needed
        finally:
            # `reset` puts back the value from before `set` (like leaving a `with` block).
            current_replay.reset(token)
        answers.append(await run_statement(alias, statement.compiler, statement.result_type))


def capture(method: Callable[[], object]) -> StatementNeeded:
    """Run Django's sync `method` until its first statement, and return that
    statement (with its compiler) instead of running it.

    For methods that must not run twice (`Model.save()` computes defaults and
    `auto_now` dates): the caller runs the statement and finishes the work.
    """
    token = current_replay.set(Replay())
    try:
        method()
    except StatementNeeded as needed:
        return needed
    finally:
        current_replay.reset(token)
    raise RuntimeError(f"{method!r} finished without running a statement")


async def run_first_statement(alias: str, method: Callable[[], object]) -> Any:
    """Run Django's sync `method` until its first statement, run that
    statement on djox, and return its answer, shaped like `execute_sql()`
    returns it (a row for a count, a number of rows for an update...).

    The rest of `method` doesn't run: the caller turns the answer into the
    result. It is cheaper than `replay()`, for methods whose last step is
    simple (`count()` returns the first column of the row).
    """
    statement = capture(method)
    return await run_statement(alias, statement.compiler, statement.result_type)


async def run_statement(alias: str, sql_compiler: "compiler.SQLCompiler", result_type: str | None) -> Any:
    """Compile and run a statement on djox, and shape its answer like
    Django's `SQLCompiler.execute_sql(result_type)` would."""
    result_type = result_type or NO_RESULTS
    try:
        sql, parameters = sql_compiler.as_sql()
        if not sql:
            raise EmptyResultSet
    except EmptyResultSet:
        # Django knows the answer without asking the database (a filter like `id__in=[]`).
        return iter([]) if result_type == MULTI else None

    if result_type == CURSOR:
        raise NotImplementedError("djox can't hand out a cursor to async code")
    if result_type in (ROW_COUNT, NO_RESULTS):
        # A write: if it fails inside an `atomic()` block, the block must
        # roll back, like when Django runs it.
        with _running.mark_for_rollback_on_error(alias):
            rows_affected = await _running.execute(alias, sql, parameters)
        return rows_affected if result_type == ROW_COUNT else None

    rows = await _running.fetch_all(alias, sql, parameters)
    if result_type == SINGLE:
        if not rows:
            return None
        return rows[0][0 : sql_compiler.col_count]
    # MULTI: a list of chunks of rows. Columns Django added only for ordering
    # (`extra(order_by=...)`) are cut off, like Django's `cursor_iter()` does.
    if sql_compiler.has_extra_select:
        rows = [row[0 : sql_compiler.col_count] for row in rows]
    return [rows]


async def run_insert(alias: str, insert_compiler: "SQLInsertCompiler", returning_fields: Any) -> list[Any]:
    """Run an `INSERT` on djox and return the rows of its `RETURNING` clause
    (converted to Python values), like `SQLInsertCompiler.execute_sql()`.

    SQLite (3.35+) supports `RETURNING`, so the ids come back with the insert.
    """
    insert_compiler.returning_fields = returning_fields
    rows: list[Any] = []
    with _running.mark_for_rollback_on_error(alias):
        for sql, parameters in insert_compiler.as_sql():
            if returning_fields:
                rows.extend(await _running.fetch_all(alias, sql, parameters))
            else:
                await _running.execute(alias, sql, parameters)
    if not returning_fields:
        return []
    table = insert_compiler.query.get_meta().db_table
    columns = [returning_field.get_col(table) for returning_field in returning_fields]
    converters = insert_compiler.get_converters(columns)
    if converters:
        return list(insert_compiler.apply_converters(rows, converters))
    return rows


class SQLCompiler(compiler.SQLCompiler):
    """Django's `SELECT` compiler, which can hand its statement to a replay."""

    def execute_sql(
        self,
        result_type: Any = MULTI,
        chunked_fetch: bool = False,
        chunk_size: int = GET_ITERATOR_CHUNK_SIZE,
    ) -> Any:
        replay_in_progress = current_replay.get()
        if replay_in_progress is None:
            return super().execute_sql(result_type, chunked_fetch, chunk_size)
        return replay_in_progress.take_answer(StatementNeeded(self, result_type))


class SQLInsertCompiler(compiler.SQLInsertCompiler, SQLCompiler):
    """Django's `INSERT` compiler, which can hand its statement to `capture()`."""

    def execute_sql(self, returning_fields: Any = None) -> Any:  # type: ignore[override]  # Django's own signature
        replay_in_progress = current_replay.get()
        if replay_in_progress is None:
            return super().execute_sql(returning_fields)
        # Remembered for `run_insert()`: Django's `execute_sql()` would set it too.
        self.returning_fields = returning_fields
        return replay_in_progress.take_answer(StatementNeeded(self, None))


# These three only need the replay-aware `execute_sql()` of `SQLCompiler`
# above. A class with two parents (`class C(A, B)`) looks a method up in `A`
# first, then in `B`: Django's `UPDATE` compiler keeps its own `execute_sql()`,
# which calls ours through `super()`.


class SQLDeleteCompiler(compiler.SQLDeleteCompiler, SQLCompiler):
    """Django's `DELETE` compiler."""


# django-stubs gives the two parents' `execute_sql()` different signatures.
class SQLUpdateCompiler(compiler.SQLUpdateCompiler, SQLCompiler):  # type: ignore[misc]
    """Django's `UPDATE` compiler."""


class SQLAggregateCompiler(compiler.SQLAggregateCompiler, SQLCompiler):
    """Django's compiler for aggregates over a subquery."""
