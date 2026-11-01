"""Running a `SimpleQuery`: parameters, the statement of its shape, then results.

Each function returns `None` when the fast lane leaves the query to Django;
`djox.QuerySet` then runs it the safe way, with the same result.

The SQL runs through `djox._running`, like every query of the safe lane: in
the open sync transaction if there is one, with Django's exceptions, and in
Django's query log.
"""

from typing import Any

from django.db import models
from django.db.models.query import ModelIterable
from django.db.models.signals import post_init, pre_init

from djox import _running
from djox._fast_lane.statements import Kind, Statement, statement_for, values_fit_their_columns


async def fetch_results(queryset: "models.QuerySet[Any]") -> list[Any] | None:
    """The model instances `queryset` returns, or `None`."""
    if not can_build_instances(queryset):
        return None
    simple_query = queryset._simple_query  # type: ignore[attr-defined]  # djox.QuerySet's
    if simple_query.matches_nothing:
        return []
    alias = queryset.db
    found = prepared_statement(queryset, "rows", alias)
    if found is None:
        return None
    statement, parameters = found
    assert statement.instance_builder is not None, "a statement for rows has one"
    rows = await _running.fetch_all(alias, statement.sql, parameters)
    fetch_mode = getattr(queryset, "_fetch_mode", None)  # Django 6.1 and later
    return statement.instance_builder.build(rows, alias, fetch_mode)


async def count(queryset: "models.QuerySet[Any]") -> int | None:
    """How many rows `queryset` has, or `None`."""
    if queryset._simple_query.matches_nothing:  # type: ignore[attr-defined]
        return 0
    alias = queryset.db
    found = prepared_statement(queryset, "count", alias)
    if found is None:
        return None
    statement, parameters = found
    rows = await _running.fetch_all(alias, statement.sql, parameters)
    return int(rows[0][0])  # type: ignore[arg-type]  # COUNT(*) is an integer


async def exists(queryset: "models.QuerySet[Any]") -> bool | None:
    """Whether `queryset` has any row, or `None`."""
    if queryset._simple_query.matches_nothing:  # type: ignore[attr-defined]
        return False
    alias = queryset.db
    found = prepared_statement(queryset, "exists", alias)
    if found is None:
        return None
    statement, parameters = found
    rows = await _running.fetch_all(alias, statement.sql, parameters)
    return bool(rows)


def prepared_statement(queryset: "models.QuerySet[Any]", kind: Kind, alias: str) -> tuple[Statement, list[Any]] | None:
    """The statement for `queryset`'s shape and its parameters, or `None`."""
    simple_query = queryset._simple_query  # type: ignore[attr-defined]
    # Checked first: Django writes no SQL (or other SQL) for such values.
    if not values_fit_their_columns(simple_query, alias):
        return None
    statement = statement_for(queryset, simple_query, kind, alias)
    if statement is None:
        return None
    return statement, statement.parameters(simple_query, alias)


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
