"""The SQL of each query shape, written once by Django and kept.

Two queries with the same shape (see `QueryRecipe.shape`) have the same
SQL, with different parameters. So the first time the fast lane meets a
shape, it lets Django build the query from the recipe and write the SQL,
exactly as Django's own methods would. It keeps:

- the SQL,
- where each parameter comes from (`parameters.py`): which of the query's
  values makes it, or a value fixed by the shape,
- for queries that return rows, a `ResultBuilder` (`results.py`).

Every later query of that shape skips Django's query building and
compiler entirely. If the fast lane can't tell where the parameters come
from, the shape is remembered as one it leaves to Django (or, if this
query's values were only in doubt, it tries again with the next query).

| Kind          | SQL Django writes                       | Used by                          |
|---------------|-----------------------------------------|----------------------------------|
| `"rows"`      | the `SELECT` of the results             | `for`, `get()`, `first()`... and their `a`-versions |
| `"count"`     | `SELECT COUNT(*) ...` (`query.get_count()`) | `count()`, `acount()`        |
| `"exists"`    | `SELECT 1 ... LIMIT 1` (`query.has_results()`) | `exists()`, `contains()`... |
| `"aggregate"` | `SELECT SUM(...) ...` (`aggregate()`)   | `aggregate()`, `aaggregate()`    |
| `"update"`    | `UPDATE ... SET ...` (`update()`)       | `update()`, `aupdate()`          |
| `"delete"`    | `DELETE FROM ...` (`_raw_delete()`)     | `delete()`, `adelete()` (a "fast delete") |
"""

from dataclasses import dataclass
from typing import Any, Literal

from django.core.exceptions import EmptyResultSet, FullResultSet
from django.core.signals import setting_changed
from django.db import connections, models

from djox._core import ResultBuilder
from djox._fast_lane.arguments import timezone_names
from djox._fast_lane.parameters import AMBIGUOUS, Fixed, Source, literals_of, parameter_sources
from djox._fast_lane.recipe import QueryRecipe, QueryValue, Step
from djox._fast_lane.results import aggregate_builder, rows_builder, tuple_order
from djox.backends.sqlite3 import compiler

Kind = Literal["rows", "count", "exists", "aggregate", "update", "delete"]


@dataclass(frozen=True)
class Statement:
    """The SQL of one query shape, and how to make its parameters and results."""

    sql: str
    """Django's SQL, with `%s` placeholders for the parameters."""

    sources: tuple[Source, ...]
    """Where each parameter comes from: the position of the value that makes
    it, or a `Fixed` parameter (see `parameters.py`)."""

    result_builder: ResultBuilder | None = None
    """For `"rows"` and `"aggregate"`: builds the results. `None` for the other kinds."""

    related_models: tuple[type[models.Model], ...] = ()
    """For `"rows"`: the models whose instances `select_related()` adds. The
    fast lane checks before each query that the builder may still make them
    (no signal receiver was connected since, see `running.builds_like_django()`)."""

    tuple_order: tuple[int, ...] | None = None
    """For `values_list()` on Django 6.0 and earlier: the order of the
    columns in each tuple, when not the order of the `SELECT` (see `results.tuple_order()`)."""

    empty_aggregate: dict[str, Any] | None = None
    """For `"aggregate"`: what `aggregate()` returns if the database returns
    no row (Django's `empty_result_set_value` of each aggregate)."""

    def parameters(self, values: tuple[QueryValue, ...], connection: Any) -> list[Any]:
        """The parameters to run this statement with, for a query's `values`.
        `connection` is the alias's Django connection (its `DatabaseWrapper`)."""
        parameters = []
        for source in self.sources:
            if isinstance(source, Fixed):
                parameters.append(source.value)
            else:
                parameters.append(values[source].parameter(connection))
        return parameters


# Shapes the fast lane leaves to Django are remembered too, so it doesn't ask again.
LEFT_TO_DJANGO = "left to Django"

# The statements by shape. A program has a few dozen shapes, rarely more; a
# program that makes new ones all the time (a different `LIMIT` per page,
# say) empties the cache when it is full, rather than growing forever.
statements: dict[tuple[Any, ...], Statement | str] = {}
MAX_STATEMENTS = 2000

# How many queries of a shape were in doubt (`parameters.AMBIGUOUS`). After
# `MAX_ATTEMPTS`, the shape is left to Django: its values are always alike.
attempts: dict[tuple[Any, ...], int] = {}
MAX_ATTEMPTS = 10


def statement_for(
    queryset: "models.QuerySet[Any]",
    recipe: QueryRecipe,
    kind: Kind,
    alias: str,
    last_step: Step | None = None,
) -> Statement | None:
    """The statement for `recipe`'s shape, written by Django the first
    time, or `None` if the fast lane leaves this shape to Django.

    `last_step` is the call that runs the query, when it has arguments:
    `update(...)` or `aggregate(...)`.
    """
    key = statement_key(queryset.model, recipe, kind, alias, last_step)
    found = statements.get(key)
    if isinstance(found, Statement):
        return found
    if found is not None:
        return None  # left to Django
    written = write_statement(queryset, recipe, kind, alias, last_step, key)
    if written == AMBIGUOUS:
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] < MAX_ATTEMPTS:
            return None  # Django's way for this query; the next may teach the fast lane
        written = LEFT_TO_DJANGO
    if len(statements) >= MAX_STATEMENTS:
        statements.clear()
        attempts.clear()
    statements[key] = written
    return written if isinstance(written, Statement) else None


def statement_key(
    model: type[models.Model], recipe: QueryRecipe, kind: Kind, alias: str, last_step: Step | None
) -> tuple[Any, ...]:
    """What decides a statement: the model, the database, the kind, the shapes,
    and for a query with expressions, the current time zone (some of Django's
    expressions write it into the SQL or its parameters, like `TruncDay()`)."""
    last_shape = last_step.shape if last_step is not None else None
    uses_expressions = recipe.uses_expressions or (last_step is not None and last_step.uses_expressions)
    time_zone = timezone_names() if uses_expressions else ()
    return (model, alias, kind, recipe.shape, last_shape, time_zone)


def values_of(recipe: QueryRecipe, last_step: Step | None) -> tuple[QueryValue, ...]:
    """The values of a query: those of the call that runs it (an `UPDATE`'s
    `SET` and an `aggregate()` come before the filters in the SQL), then the recipe's."""
    if last_step is None:
        return recipe.values
    return (*last_step.values, *recipe.values)


def write_statement(
    queryset: "models.QuerySet[Any]",
    recipe: QueryRecipe,
    kind: Kind,
    alias: str,
    last_step: Step | None,
    key: tuple[Any, ...],
) -> Statement | str:
    """Let Django build `recipe`'s query and write its SQL (see the module docs).
    Returns the statement, `LEFT_TO_DJANGO`, or `AMBIGUOUS`."""
    # A copy of the QuerySet with this recipe; asking for its `query` makes Django build it.
    django_queryset = queryset._with_recipe(recipe)  # type: ignore[attr-defined]  # djox.QuerySet's
    result_builder = None
    related_models: tuple[type[models.Model], ...] = ()
    order = None
    empty_aggregate = None
    try:
        query = django_queryset.query
        if kind == "rows":
            sql_compiler = query.get_compiler(using=alias)
            sql, django_parameters = sql_compiler.as_sql()
            returns_values = django_queryset._fields is not None
            found = rows_builder(queryset.model, sql_compiler, returns_values)
            if found is None:
                return LEFT_TO_DJANGO
            result_builder, related_models = found
            if returns_values:
                order = tuple_order(django_queryset, query)
        else:
            # These methods run their statement: capture it instead.
            statement = compiler.capture(lambda: run_django_method(django_queryset, query, kind, alias, last_step))
            sql, django_parameters = statement.compiler.as_sql()
            if kind == "aggregate":
                result_builder = aggregate_builder(statement.compiler)
                if result_builder is None:
                    return LEFT_TO_DJANGO
                outer_query = statement.compiler.query
                empty_aggregate = {}
                for name, expression in outer_query.annotation_select.items():
                    empty_aggregate[name] = expression.empty_result_set_value
    except (EmptyResultSet, FullResultSet, RuntimeError):
        # Django answers this one without SQL, from the values: let it.
        # (`RuntimeError`: `capture()` found no statement to take.)
        return LEFT_TO_DJANGO

    # `exists()` selects one constant before the other parameters (`SELECT %s AS "a"`, with 1).
    fixed = [Fixed(parameter) for parameter in django_parameters[:1]] if kind == "exists" else []
    other_parameters = list(django_parameters[len(fixed) :])
    literals = literals_of(key)
    sources = parameter_sources(other_parameters, values_of(recipe, last_step), connections[alias], literals)
    if sources is None:
        return LEFT_TO_DJANGO
    if sources == AMBIGUOUS:
        return AMBIGUOUS
    assert isinstance(sources, tuple)
    return Statement(sql, (*fixed, *sources), result_builder, related_models, order, empty_aggregate)


def run_django_method(
    django_queryset: "models.QuerySet[Any]", query: Any, kind: Kind, alias: str, last_step: Step | None
) -> object:
    """Run the Django method of `kind` (inside `compiler.capture()`, which stops it at its statement)."""
    if kind == "count":
        return query.get_count(using=alias)
    if kind == "exists":
        return query.has_results(using=alias)
    if kind == "aggregate":
        assert last_step is not None, "aggregate() has its arguments"
        return models.QuerySet.aggregate(django_queryset, *last_step.args, **last_step.kwargs)
    if kind == "update":
        assert last_step is not None, "update() has its arguments"
        return models.QuerySet.update(django_queryset, **last_step.kwargs)
    query.clear_ordering(force=True)  # what Django's `delete()` does first
    return django_queryset._raw_delete(alias)


def values_fit_their_columns(values: tuple[QueryValue, ...], connection: Any) -> bool:
    """Whether every integer fits its column, by Django's rules.

    Django's integer lookups know without asking that a number bigger than the
    column can hold matches nothing (`pages=2**70`), or everything (`pages__lt=2**70`),
    and run no SQL, or other SQL. The fast lane leaves those queries to Django.
    """
    for value in values:
        if not value.checks_integer_range:
            continue
        smallest, largest = connection.ops.integer_field_range(value.field.get_internal_type())
        number = value.value
        if (smallest is not None and number < smallest) or (largest is not None and number > largest):
            return False
    return True


def forget_statements(**_details: object) -> None:
    """Forget every statement, when a test changes Django's settings: the SQL
    or the converters might change with them."""
    statements.clear()
    attempts.clear()


setting_changed.connect(forget_statements)
