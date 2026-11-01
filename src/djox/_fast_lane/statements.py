"""The SQL of each query shape, written once by Django and kept.

Two queries with the same shape (see `SimpleQuery.shape`) have the same SQL,
with different parameters. So the first time the fast lane meets a shape, it
lets Django build the query and write the SQL, exactly as Django's own
methods would. It keeps the SQL, and for queries that return rows, a
`ResultBuilder` set up from Django's compiler (which columns go to which
attribute or key, with which converters, and which of those Rust can do).
Every later query of that shape skips
Django's query building and compiler entirely.

Before keeping a statement, the fast lane checks that it computes the same
parameters as Django did, value by value. If not (a field that prepares its
values in its own way, say), the shape is remembered as one the fast lane
leaves to Django.

| Kind       | SQL Django writes                       | Used by                          |
|------------|-----------------------------------------|----------------------------------|
| `"rows"`   | the `SELECT` of the model's columns     | `for`, `get()`, `first()`... and their `a`-versions |
| `"count"`  | `SELECT COUNT(*) ...` (`query.get_count()`) | `count()`, `acount()`        |
| `"exists"` | `SELECT 1 ... LIMIT 1` (`query.has_results()`) | `exists()`, `contains()`... |
| `"update"` | `UPDATE ... SET ...` (`update()`)       | `update()`, `aupdate()`          |
| `"delete"` | `DELETE FROM ...` (`_raw_delete()`)     | `delete()`, `adelete()` (a "fast delete") |
"""

from dataclasses import dataclass
from typing import Any, Literal

from django.core.exceptions import EmptyResultSet, FullResultSet
from django.core.signals import setting_changed
from django.db import connections, models
from django.db.models.base import ModelState

from djox._core import RelatedModel, ResultBuilder
from djox._fast_lane.conversions import conversion_for
from djox._fast_lane.simple_query import SimpleQuery, Update
from djox.backends.sqlite3 import compiler

Kind = Literal["rows", "count", "exists", "update", "delete"]


@dataclass(frozen=True)
class Statement:
    """The SQL of one query shape, and how to build its objects."""

    sql: str
    """Django's SQL, with `%s` placeholders for the parameters."""

    result_builder: ResultBuilder | None
    """For `"rows"`: builds the results (instances, tuples or dicts). `None` for the other kinds."""

    related_models: tuple[type[models.Model], ...] = ()
    """For `"rows"`: the models whose instances `select_related()` adds. The
    fast lane checks before each query that the builder may still make them
    (no signal receiver was connected since, see `running.builds_like_django()`)."""

    select_parameters: tuple[Any, ...] = ()
    """Parameters Django puts in the `SELECT` clause, before the filters' ones:
    `exists()` selects `%s` with `1`. The same for every query of the shape."""

    writes_booleans_in_sql: bool = False
    """Whether Django wrote `in_print=True` as `WHERE "in_print"` rather than
    `WHERE "in_print" = %s` (Django 6.0 and earlier do). Then those values are
    no parameters: they are in the SQL (and in the shape)."""

    def parameters(self, simple_query: SimpleQuery, alias: str, updates: tuple[Update, ...] = ()) -> list[Any]:
        """The parameters to run this statement with, for `simple_query`'s
        values (and, for an `UPDATE`, the values it sets, which come first)."""
        filter_parameters = parameters_for(simple_query, alias, self.writes_booleans_in_sql)
        return [*self.select_parameters, *update_parameters(updates, alias), *filter_parameters]


# Shapes the fast lane leaves to Django are remembered too, so it doesn't ask again.
LEFT_TO_DJANGO = "left to Django"

# The statements by shape. A program has a few dozen shapes, rarely more; a
# program that makes new ones all the time (a different `LIMIT` per page,
# say) empties the cache when it is full, rather than growing forever.
statements: dict[tuple[Any, ...], Statement | str] = {}
MAX_STATEMENTS = 2000


def statement_for(
    queryset: "models.QuerySet[Any]",
    simple_query: SimpleQuery,
    kind: Kind,
    alias: str,
    updates: tuple[Update, ...] = (),
) -> Statement | None:
    """The statement for `simple_query`'s shape, written by Django the first
    time, or `None` if the fast lane leaves this shape to Django. `updates`
    are the values an `UPDATE` sets."""
    update_shapes = tuple(update.shape for update in updates)
    key = (queryset.model, alias, kind, simple_query.shape, update_shapes)
    found = statements.get(key)
    if found is None:
        found = write_statement(queryset, simple_query, kind, alias, updates)
        if len(statements) >= MAX_STATEMENTS:
            statements.clear()
        statements[key] = found
    if isinstance(found, Statement):
        return found
    return None


def write_statement(
    queryset: "models.QuerySet[Any]",
    simple_query: SimpleQuery,
    kind: Kind,
    alias: str,
    updates: tuple[Update, ...] = (),
) -> Statement | str:
    """Let Django build `simple_query` and write its SQL (see the module docs)."""
    # A copy of the QuerySet with this note; asking for its `query` makes Django build it.
    django_queryset = queryset._with_simple_query(simple_query)  # type: ignore[attr-defined]  # djox.QuerySet's
    query = django_queryset.query
    result_builder = None
    related_models: tuple[type[models.Model], ...] = ()
    try:
        if kind == "rows":
            sql_compiler = query.get_compiler(using=alias)
            sql, django_parameters = sql_compiler.as_sql()
            found = result_builder_for(queryset.model, sql_compiler, simple_query.values_fields)
            if found is None:
                return LEFT_TO_DJANGO
            result_builder, related_models = found
        else:
            # These methods run their statement: capture it instead.
            if kind == "count":
                statement = compiler.capture(lambda: query.get_count(using=alias))
            elif kind == "exists":
                statement = compiler.capture(lambda: query.has_results(using=alias))
            elif kind == "update":
                values = {update.name: update.value for update in updates}
                statement = compiler.capture(lambda: django_queryset.update(**values))
            else:
                query.clear_ordering(force=True)  # what Django's `delete()` does first
                statement = compiler.capture(lambda: django_queryset._raw_delete(alias))
            sql, django_parameters = statement.compiler.as_sql()
    except (EmptyResultSet, FullResultSet, RuntimeError):
        # Django answers this one without SQL, from the values: let it.
        # (`RuntimeError`: `capture()` found no statement to take.)
        return LEFT_TO_DJANGO

    # Check that the fast lane computes Django's parameters, one of two ways.
    # `exists()` selects one constant before them (`SELECT %s AS "a"`, with 1).
    select_parameters = tuple(django_parameters[:1]) if kind == "exists" else ()
    for writes_booleans_in_sql in (False, True):
        candidate = Statement(sql, result_builder, related_models, select_parameters, writes_booleans_in_sql)
        if same_values(list(django_parameters), candidate.parameters(simple_query, alias, updates)):
            return candidate
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


def result_builder_for(
    model: type[models.Model], sql_compiler: Any, values_fields: tuple[str, ...] | None
) -> tuple[ResultBuilder, tuple[type[models.Model], ...]] | None:
    """A `ResultBuilder` for the rows of `sql_compiler`'s `SELECT`, and the
    models of `select_related()` it makes, or `None` if the rows aren't simply
    the model's own columns (or, for `values()`, the fields asked for), plus
    whole related models.

    After `as_sql()`, the compiler knows what it selected (`select`), which
    model the columns belong to (`klass_info`) and how Django converts each
    one (`get_converters()`): the same things Django's iterable classes read.
    """
    columns = [selected[0] for selected in sql_compiler.select]
    if sql_compiler.has_extra_select:
        return None  # columns added only for ordering
    related: list[RelatedModel] = []
    related_models: list[type[models.Model]] = []
    if values_fields is None:
        # Model instances: one attribute per column, every field of the model,
        # then every field of each related model (`select_related()`).
        klass_info = sql_compiler.klass_info
        if sql_compiler.annotation_col_map or klass_info is None or klass_info["model"] is not model:
            return None
        if whole_model_columns(klass_info, columns) != (0, len(model._meta.concrete_fields)):
            return None  # some fields are deferred (`only()`, `defer()`): Django's `from_db()` handles that
        found_related = related_models_for(klass_info, columns, related_models)
        if found_related is None:
            return None
        related = found_related
        column_count = len(model._meta.concrete_fields)
        for related_model in related_models:
            column_count += len(related_model._meta.concrete_fields)
        if column_count != len(columns):
            return None  # columns that belong to no model
        names = [column.target.attname for column in columns]
    else:
        # `values()` and `values_list()`: one key (or tuple position) per field
        # asked for, named like Django's `ValuesIterable` names them. (Django
        # 6.1 selects them `AS` their names, which the compiler lists in
        # `annotation_col_map`; real annotations are in `annotation_select`.)
        query = sql_compiler.query
        names = list(query.values_select)
        if query.extra_select or query.annotation_select or len(names) != len(columns):
            return None
    found_converters = sql_compiler.get_converters(columns)
    converters = []
    conversions: list[tuple[Any, ...] | None] = []
    for index in range(len(columns)):
        column_converters = found_converters.get(index)  # `(functions, expression)`, or `None`
        converters.append(column_converters)
        if column_converters is None:
            conversions.append(None)
        else:
            functions, expression = column_converters
            conversions.append(conversion_for(functions, expression, sql_compiler.connection))
    if values_fields is not None:
        return ResultBuilder(names, converters, conversions, sql_compiler.connection), ()
    builder = ResultBuilder(names, converters, conversions, sql_compiler.connection, model, ModelState, related)
    return builder, tuple(related_models)


def related_models_for(
    klass_info: dict[str, Any], columns: list[Any], related_models: list[type[models.Model]]
) -> list[RelatedModel] | None:
    """The `RelatedModel`s `select_related()` adds below `klass_info` (and,
    in turn, below them), or `None` if one isn't simple enough. Each model is
    also added to `related_models`.

    Django describes each related model in `klass_info["related_klass_infos"]`,
    for its `RelatedPopulator`: the model, its columns, and the functions that
    cache it on its parent (`local_setter`) and its parent on it (`remote_setter`).
    """
    related = []
    for related_info in klass_info.get("related_klass_infos", []):
        related_model = related_info["model"]
        model_columns = whole_model_columns(related_info, columns)
        # `from_parent`: a child model of multi-table inheritance, whose
        # columns are mixed with its parent's. Django's `RelatedPopulator` reorders them.
        if related_info["from_parent"] or model_columns is None:
            return None
        first_column, end_column = model_columns
        if end_column - first_column != len(related_model._meta.concrete_fields):
            return None  # some fields are deferred (`only()`)
        names = [columns[index].target.attname for index in range(first_column, end_column)]
        # Django checks the first column of the primary key (a composite
        # primary key's columns are all `NULL` when one is).
        primary_key_name = related_model._meta.pk_fields[0].attname
        if primary_key_name not in names:
            return None
        below = related_models_for(related_info, columns, related_models)
        if below is None:
            return None
        related_models.append(related_model)
        related.append(
            RelatedModel(
                related_model,
                first_column,
                end_column,
                first_column + names.index(primary_key_name),
                related_info["local_setter"],
                related_info["remote_setter"],
                below,
            )
        )
    return related


def whole_model_columns(klass_info: dict[str, Any], columns: list[Any]) -> tuple[int, int] | None:
    """Where a model's columns start and end in the row, `(first, end)` like
    `row[first:end]`, if they are side by side and are plain columns. Otherwise `None`."""
    select_fields = list(klass_info["select_fields"])
    if not select_fields:
        return None
    first_column, end_column = select_fields[0], select_fields[-1] + 1
    if select_fields != list(range(first_column, end_column)):
        return None
    for index in select_fields:
        if getattr(columns[index], "target", None) is None:
            return None
    return first_column, end_column


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


def update_parameters(updates: tuple[Update, ...], alias: str) -> list[Any]:
    """The parameters of an `UPDATE`'s `SET`, prepared like Django's `UPDATE`
    compiler prepares them (`get_db_prep_save()`). `None` has none: it is `NULL` in the SQL."""
    connection = connections[alias]
    parameters = []
    for update in updates:
        if update.value is not None:
            parameters.append(update.field.get_db_prep_save(update.value, connection))
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
