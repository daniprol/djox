"""Hooks: functions djox calls before and after each SQL statement.

A hook can watch the statements (time them, log them, count them per
request) or change them (add a comment for tracing, block writes to a
read-only database):

```python
import djox


def tag_with_request(query: djox.Query) -> djox.Query:
    return query.with_sql(f"/* request {current_request_id()} */ {query.sql}")


def warn_if_slow(query: djox.Query, finished: djox.QueryFinished) -> None:
    if finished.seconds > 0.1:
        logger.warning("slow query (%.3fs): %s", finished.seconds, query.sql)


djox.add_hooks(djox.Hooks(before_query=tag_with_request, after_query=warn_if_slow))
```

| Hook                            | When                                    | Return                                  |
|---------------------------------|-----------------------------------------|-----------------------------------------|
| `before_query(query)`           | before each statement                   | `None`, or a new `Query` to run instead |
| `after_query(query, finished)`  | after each statement, also when it failed | `None`                                |

They see every statement run on djox's `ENGINE`, from sync or async code:
Django's own ORM (through Django's cursors), and every `djox.QuerySet`
method, the fast lane included. (`djox.Database` is the layer below the ORM
and has no hooks.)

Hooks are plain functions, not `async` ones: they run inside every query,
also from sync code, where nothing can be awaited. They run in the order they
were added; each `before_query` sees the query the previous one returned.
Raising in `before_query` stops the statement: the caller gets the error.
Django's signals (`pre_save`, `post_delete`...) keep working as always.
"""

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class Query:
    """One SQL statement about to run (or that just ran)."""

    alias: str
    """The Django database alias, like `"default"`."""

    sql: str
    """The SQL, with Django's `%s` placeholders."""

    parameters: Sequence[Any]
    """The values for the placeholders. For `many`, one sequence of values per run."""

    many: bool = False
    """Whether the statement runs once per item of `parameters` (`executemany()`)."""

    def with_sql(self, sql: str) -> "Query":
        """The same query with other SQL, for `before_query` to return."""
        return replace(self, sql=sql)

    def with_parameters(self, parameters: Sequence[Any]) -> "Query":
        """The same query with other parameters, for `before_query` to return."""
        return replace(self, parameters=parameters)


@dataclass(frozen=True)
class QueryFinished:
    """How a statement went, for `after_query`."""

    seconds: float
    """How long it took, waiting for the database included."""

    error: BaseException | None = None
    """The error it raised, or `None` if it worked."""


@dataclass(frozen=True)
class Hooks:
    """Hooks made of plain functions: `djox.add_hooks(djox.Hooks(...))`.

    Any object with some of these methods works too (see the module docs).
    """

    before_query: Callable[[Query], Query | None] | None = None
    after_query: Callable[[Query, QueryFinished], None] | None = None


# Every hook added with `add_hooks()`, in order. Each query reads this list, so
# it is replaced by a new list on every change, never changed in place: a query
# running on another thread meanwhile keeps the list it started with.
added_hooks: tuple[Any, ...] = ()


def add_hooks(hooks: Any) -> None:
    """Call `hooks` (a `Hooks`, or any object with `before_query` and/or
    `after_query` methods) for every statement from now on."""
    global added_hooks  # `global`: this function changes the module's variable, not a local one
    added_hooks = (*added_hooks, hooks)


def remove_hooks(hooks: Any) -> None:
    """Stop calling `hooks`. Does nothing if they weren't added."""
    global added_hooks
    added_hooks = tuple(added for added in added_hooks if added is not hooks)


def run_before_query(query: Query) -> Query:
    """Run every `before_query` hook, in order; the query to run."""
    for hooks in added_hooks:
        hook = getattr(hooks, "before_query", None)
        if hook is not None:
            changed = hook(query)
            if changed is not None:
                query = changed
    return query


def run_after_query(query: Query, finished: QueryFinished) -> None:
    """Run every `after_query` hook, in order."""
    for hooks in added_hooks:
        hook = getattr(hooks, "after_query", None)
        if hook is not None:
            hook(query, finished)


@contextmanager
def around_query(alias: str, sql: str, parameters: Sequence[Any], many: bool = False) -> Iterator[Query]:
    """Run the hooks around one statement: `before_query` now, `after_query`
    when the `with` block ends. The block runs the query it is given:

        with around_query(alias, sql, parameters) as query:
            rows = run(query.sql, query.parameters)

    (`@contextmanager` turns this generator into a `with` block: the block
    runs at the `yield`.)
    """
    query = run_before_query(Query(alias, sql, parameters, many))
    started = time.perf_counter()
    try:
        yield query
    except BaseException as error:
        run_after_query(query, QueryFinished(time.perf_counter() - started, error))
        raise
    run_after_query(query, QueryFinished(time.perf_counter() - started))


def execute_with_hooks(execute: Callable[..., Any], sql: str, parameters: Any, many: bool, context: Any) -> Any:
    """The hooks for sync code: djox's `DatabaseWrapper` adds this to Django's
    `execute_wrappers`, which every Django cursor calls around each statement
    (Django's own way to wrap queries, `connection.execute_wrapper()`)."""
    if not added_hooks:
        return execute(sql, parameters, many, context)
    with around_query(context["connection"].alias, sql, parameters, many) as query:
        return execute(query.sql, query.parameters, many, context)
