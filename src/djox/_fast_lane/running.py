"""Running a `SimpleQuery`: the statement of its shape, its parameters, then results.

`prepare()` does everything but running the SQL, and returns `None` when the
fast lane leaves the query to Django; `djox.QuerySet` then runs it the way it
would without the fast lane, with the same result. A `PreparedQuery` runs the
same way from async code (`await prepared.fetch_rows()`) and from sync code
(`prepared.fetch_rows_now()`), and turns the rows into the answer:

```python
prepared = prepare(queryset, "count")
if prepared is not None:
    return prepared.count(await prepared.fetch_rows())
```

The SQL runs through `djox._running`, like every query of the safe lane: in
the open transaction if there is one, with Django's exceptions, and in
Django's query log.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.db import models
from django.db.models.query import ModelIterable
from django.db.models.signals import post_init, pre_init

from djox import _running
from djox._fast_lane.statements import Kind, Statement, statement_for, values_fit_their_columns

if TYPE_CHECKING:
    from djox._core import Row


@dataclass(frozen=True)
class PreparedQuery:
    """A simple query, ready to run: its statement, its parameters, its database."""

    statement: Statement | None
    """`None` when Django knows without asking that nothing matches
    (`id__in=[]`, `[5:5]`): then no SQL runs."""

    parameters: list[Any]
    alias: str

    async def fetch_rows(self) -> "list[Row]":
        """Run the statement on djox, from async code."""
        if self.statement is None:
            return []
        return await _running.fetch_all(self.alias, self.statement.sql, self.parameters)

    def fetch_rows_now(self) -> "list[Row]":
        """Run the statement on this thread's Django connection, from sync code."""
        if self.statement is None:
            return []
        return _running.fetch_all_now(self.alias, self.statement.sql, self.parameters)

    def instances(self, rows: "list[Row]", queryset: "models.QuerySet[Any]") -> list[Any]:
        """The model instances of `rows` (for a query prepared as `"rows"`)."""
        if self.statement is None:
            return []
        assert self.statement.instance_builder is not None, "a statement for rows has one"
        fetch_mode = getattr(queryset, "_fetch_mode", None)  # Django 6.1 and later
        return self.statement.instance_builder.build(rows, self.alias, fetch_mode)

    def count(self, rows: "list[Row]") -> int:
        """The count of `rows` (for a query prepared as `"count"`): `COUNT(*)` is the only column."""
        return int(rows[0][0]) if rows else 0  # type: ignore[arg-type]  # COUNT(*) is an integer

    def exists(self, rows: "list[Row]") -> bool:
        """Whether there is a row (for a query prepared as `"exists"`)."""
        return bool(rows)


def prepare(queryset: "models.QuerySet[Any]", kind: Kind) -> PreparedQuery | None:
    """`queryset`'s simple query, ready to run as `kind`, or `None` to leave it to Django.

    `queryset` must have a note (`queryset._simple_query`).
    """
    if kind == "rows" and not can_build_instances(queryset):
        return None
    simple_query = queryset._simple_query  # type: ignore[attr-defined]  # djox.QuerySet's
    alias = queryset.db
    if simple_query.matches_nothing:
        return PreparedQuery(None, [], alias)
    # Checked first: Django writes no SQL (or other SQL) for such values.
    if not values_fit_their_columns(simple_query, alias):
        return None
    statement = statement_for(queryset, simple_query, kind, alias)
    if statement is None:
        return None
    return PreparedQuery(statement, statement.parameters(simple_query, alias), alias)


def can_build_instances(queryset: "models.QuerySet[Any]") -> bool:
    """Whether `InstanceBuilder` makes the same instances as Django's `from_db()` would.

    It skips the model's `__init__`, so the model must not change what
    `__init__` or `from_db()` do, and no one may listen to the `pre_init` and
    `post_init` signals that `__init__` sends. Some fields listen to them
    themselves (`ImageField` with `width_field`, `GenericForeignKey`).
    """
    model = queryset.model
    if queryset._iterable_class is not ModelIterable:
        return False
    if queryset._known_related_objects:  # type: ignore[attr-defined]  # set by related managers
        return False
    fetch_mode = getattr(queryset, "_fetch_mode", None)
    if fetch_mode is not None and fetch_mode.track_peers:
        return False  # Django 6.1's FETCH_PEERS links the instances to each other
    if model.__init__ is not models.Model.__init__:
        return False
    if model.from_db.__func__ is not models.Model.from_db.__func__:  # type: ignore[attr-defined]  # a classmethod
        return False
    return not (pre_init.has_listeners(model) or post_init.has_listeners(model))
