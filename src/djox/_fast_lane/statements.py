"""The SQL of each query shape, written once by Django and kept.

Two queries with the same shape (see `SimpleQuery.shape`) have the same SQL,
with different parameters. So the first time the fast lane meets a shape, it
lets Django build the query and write the SQL, exactly as Django's own
methods would. It keeps the SQL, and for queries that return objects, an
`InstanceBuilder` set up from Django's compiler (which columns go to which
attribute, with which converters). Every later query of that shape skips
Django's query building and compiler entirely.

Before keeping a statement, the fast lane checks that it computes the same
parameters as Django did, value by value. If not (a field that prepares its
values in its own way, say), the shape is remembered as one the fast lane
leaves to Django.

| Kind       | SQL Django writes                       | Used by                          |
|------------|-----------------------------------------|----------------------------------|
| `"rows"`   | the `SELECT` of the model's columns     | `async for`, `aget()`, `afirst()`... |
| `"count"`  | `SELECT COUNT(*) ...` (`query.get_count()`) | `acount()`                   |
| `"exists"` | `SELECT 1 ... LIMIT 1` (`query.has_results()`) | `aexists()`, `acontains()`  |
"""

from dataclasses import dataclass
from typing import Any, Literal

from django.core.exceptions import EmptyResultSet, FullResultSet
from django.core.signals import setting_changed
from django.db import connections, models
from django.db.models.base import ModelState

from djox._core import InstanceBuilder
from djox._fast_lane.simple_query import SimpleQuery
from djox.backends.sqlite3 import compiler

Kind = Literal["rows", "count", "exists"]


@dataclass(frozen=True)
class Statement:
    """The SQL of one query shape, and how to build its objects."""

    sql: str
    """Django's SQL, with `%s` placeholders for the parameters."""

    instance_builder: InstanceBuilder | None
    """For `"rows"`: builds the model instances. `None` for counts and `exists`."""

    select_parameters: tuple[Any, ...] = ()
    """Parameters Django puts in the `SELECT` clause, before the filters' ones:
    `exists()` selects `%s` with `1`. The same for every query of the shape."""

    writes_booleans_in_sql: bool = False
    """Whether Django wrote `in_print=True` as `WHERE "in_print"` rather than
    `WHERE "in_print" = %s` (Django 6.0 and earlier do). Then those values are
    no parameters: they are in the SQL (and in the shape)."""

    def parameters(self, simple_query: SimpleQuery, alias: str) -> list[Any]:
        """The parameters to run this statement with, for `simple_query`'s values."""
        filter_parameters = parameters_for(simple_query, alias, self.writes_booleans_in_sql)
        return [*self.select_parameters, *filter_parameters]


# Shapes the fast lane leaves to Django are remembered too, so it doesn't ask again.
LEFT_TO_DJANGO = "left to Django"

# The statements by shape. A program has a few dozen shapes, rarely more; a
# program that makes new ones all the time (a different `LIMIT` per page,
# say) empties the cache when it is full, rather than growing forever.
statements: dict[tuple[Any, ...], Statement | str] = {}
MAX_STATEMENTS = 2000


def statement_for(
    queryset: "models.QuerySet[Any]", simple_query: SimpleQuery, kind: Kind, alias: str
) -> Statement | None:
    """The statement for `simple_query`'s shape, written by Django the first
    time, or `None` if the fast lane leaves this shape to Django."""
    key = (queryset.model, alias, kind, simple_query.shape)
    found = statements.get(key)
    if found is None:
        found = write_statement(queryset, simple_query, kind, alias)
        if len(statements) >= MAX_STATEMENTS:
            statements.clear()
        statements[key] = found
    if isinstance(found, Statement):
        return found
    return None


def write_statement(
    queryset: "models.QuerySet[Any]", simple_query: SimpleQuery, kind: Kind, alias: str
) -> Statement | str:
    """Let Django build `simple_query` and write its SQL (see the module docs)."""
    # A copy of the QuerySet with this note; asking for its `query` makes Django build it.
    django_queryset = queryset._with_simple_query(simple_query)  # type: ignore[attr-defined]  # djox.QuerySet's
    query = django_queryset.query
    instance_builder = None
    try:
        if kind == "rows":
            sql_compiler = query.get_compiler(using=alias)
            sql, django_parameters = sql_compiler.as_sql()
            instance_builder = instance_builder_for(queryset.model, sql_compiler)
            if instance_builder is None:
                return LEFT_TO_DJANGO
        else:
            # `get_count()` and `has_results()` run their statement: capture it instead.
            if kind == "count":
                statement = compiler.capture(lambda: query.get_count(using=alias))
            else:
                statement = compiler.capture(lambda: query.has_results(using=alias))
            sql, django_parameters = statement.compiler.as_sql()
    except (EmptyResultSet, FullResultSet, RuntimeError):
        # Django answers this one without SQL, from the values: let it.
        # (`RuntimeError`: `capture()` found no statement to take.)
        return LEFT_TO_DJANGO

    # Check that the fast lane computes Django's parameters, one of two ways.
    # `exists()` selects one constant before them (`SELECT %s AS "a"`, with 1).
    select_parameter_count = 1 if kind == "exists" else 0
    select_parameters = tuple(django_parameters[:select_parameter_count])
    filter_parameters = list(django_parameters[select_parameter_count:])
    for writes_booleans_in_sql in (False, True):
        if same_values(filter_parameters, parameters_for(simple_query, alias, writes_booleans_in_sql)):
            return Statement(sql, instance_builder, select_parameters, writes_booleans_in_sql)
    return LEFT_TO_DJANGO


def same_values(first: list[Any], second: list[Any]) -> bool:
    """Whether two lists of parameters are equal, value by value and type by
    type (`True == 1` in Python, but they aren't the same parameter)."""
    if len(first) != len(second):
        return False
    for first_value, second_value in zip(first, second, strict=True):
        if type(first_value) is not type(second_value) or first_value != second_value:
            return False
    return True


def instance_builder_for(model: type[models.Model], sql_compiler: Any) -> InstanceBuilder | None:
    """An `InstanceBuilder` for the rows of `sql_compiler`'s `SELECT`, or
    `None` if they aren't simply the model's own columns.

    After `as_sql()`, the compiler knows what it selected (`select`), which
    model the columns belong to (`klass_info`) and how Django converts each
    one (`get_converters()`): the same things Django's `ModelIterable` reads.
    """
    klass_info = sql_compiler.klass_info
    columns = [selected[0] for selected in sql_compiler.select]
    if (
        klass_info is None
        or klass_info["model"] is not model
        or klass_info.get("related_klass_infos")
        or list(klass_info["select_fields"]) != list(range(len(columns)))
        or sql_compiler.annotation_col_map
        or sql_compiler.has_extra_select
    ):
        return None
    attnames = [column.target.attname for column in columns]
    if len(attnames) != len(model._meta.concrete_fields):
        return None  # some fields are deferred (`only()`, `defer()`): Django's `from_db()` handles that
    found_converters = sql_compiler.get_converters(columns)
    converters = [found_converters.get(index) for index in range(len(columns))]
    return InstanceBuilder(model, attnames, converters, sql_compiler.connection, ModelState)


def parameters_for(simple_query: SimpleQuery, alias: str, writes_booleans_in_sql: bool = False) -> list[Any]:
    """The filters' parameters, computed like Django's lookups compute them.

    The values were prepared by their field when `filter()` ran; the database
    connection finishes them (`get_db_prep_value()`: dates to text, and so on).
    `writes_booleans_in_sql` leaves out `True` and `False` compared with `exact`
    (see `Statement.writes_booleans_in_sql`).
    """
    connection = connections[alias]
    parameters: list[Any] = []
    for condition in simple_query.conditions:
        if writes_booleans_in_sql and condition.lookup == "exact" and isinstance(condition.detail, bool):
            continue
        # Django's lookups prepare a foreign key's value with the field it points to.
        field = getattr(condition.field, "target_field", None) or condition.field
        for value in condition.values:
            parameters.append(field.get_db_prep_value(value, connection, prepared=True))
    return parameters


def values_fit_their_columns(simple_query: SimpleQuery, alias: str) -> bool:
    """Whether every integer fits its column, by Django's rules.

    Django's integer lookups know without asking that a number bigger than the
    column can hold matches nothing (`pages=2**70`), or everything (`pages__lt=2**70`),
    and run no SQL, or other SQL. The fast lane leaves those queries to Django.
    """
    connection = connections[alias]
    for condition in simple_query.conditions:
        if not condition.checks_integer_range:
            continue
        smallest, largest = connection.ops.integer_field_range(condition.field.get_internal_type())
        for value in condition.values:
            if (smallest is not None and value < smallest) or (largest is not None and value > largest):
                return False
    return True


def forget_statements(**_details: object) -> None:
    """Forget every statement, when a test changes Django's settings: the SQL
    or the converters might change with them."""
    statements.clear()


setting_changed.connect(forget_statements)
