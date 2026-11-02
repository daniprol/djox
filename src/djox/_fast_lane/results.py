"""The `ResultBuilder` of a statement: how Rust turns its rows into results.

After `as_sql()`, Django's compiler knows what each column of the `SELECT`
is (`select`), which model it belongs to (`klass_info`), which annotation
it is (`annotation_col_map`) and how Django converts it (`get_converters()`):
the same things Django's iterable classes read to build results. This file
reads them once per shape and sets up a `ResultBuilder` (`src/results.rs`)
that builds the same results, for every later query of the shape:

| Results                        | Columns                                                  |
|--------------------------------|----------------------------------------------------------|
| model instances                | the model's fields, its annotations, then each `select_related()` model's fields |
| dicts (`values()`)             | one key per field or annotation, named like Django's `ValuesIterable` |
| tuples (`values_list()`)       | the same columns, in the order of the names given         |
| one dict (`aggregate()`)       | one key per aggregate                                     |

`None` means the rows are something else (deferred fields, `extra()`...):
the fast lane then leaves the shape to Django.
"""

from typing import Any

from django.db import models
from django.db.models.base import ModelState

from djox._core import RelatedModel, ResultBuilder
from djox._fast_lane.conversions import conversion_for


def rows_builder(
    model: type[models.Model], sql_compiler: Any, returns_values: bool
) -> tuple[ResultBuilder, tuple[type[models.Model], ...]] | None:
    """A `ResultBuilder` for the rows of `sql_compiler`'s `SELECT`, and the
    models of `select_related()` it makes, or `None`. `returns_values`: the
    query was made by `values()` or `values_list()` (dicts or tuples, not instances)."""
    columns = [selected[0] for selected in sql_compiler.select]
    if sql_compiler.has_extra_select:
        return None  # columns added only for ordering
    if returns_values:
        names = values_names(sql_compiler.query)
        if names is None or len(names) != len(columns):
            return None
        return builder_for(names, columns, sql_compiler), ()

    # Model instances: one attribute per column. First every field of the
    # model, then its annotations (Django sets them as attributes too), then
    # every field of each related model (`select_related()`).
    klass_info = sql_compiler.klass_info
    if klass_info is None or klass_info["model"] is not model:
        return None
    field_count = len(model._meta.concrete_fields)
    if whole_model_columns(klass_info, columns) != (0, field_count):
        return None  # some fields are deferred (`only()`, `defer()`): Django's `from_db()` handles that
    names = [column.target.attname for column in columns[:field_count]]
    # `annotation_col_map` is `{name: column}`; Django selects annotations right after the fields.
    annotations = sorted(sql_compiler.annotation_col_map.items(), key=lambda item: item[1])
    for expected_column, (name, column) in enumerate(annotations, start=field_count):
        if column != expected_column:
            return None
        names.append(name)
    related_models: list[type[models.Model]] = []
    related = related_models_for(klass_info, columns, related_models)
    if related is None:
        return None
    column_count = len(names)
    for related_model in related_models:
        column_count += len(related_model._meta.concrete_fields)
    if column_count != len(columns):
        return None  # columns that belong to no model
    for column in columns[len(names) :]:
        names.append(column.target.attname)
    builder = builder_for(names, columns, sql_compiler, model, related)
    return builder, tuple(related_models)


def values_names(query: Any) -> list[str] | None:
    """The names of the columns of a `values()` query, like Django's `ValuesIterable`
    names them: Django 6.1 lists them in `query.selected`; earlier versions put
    the fields first, then the annotations."""
    if query.extra_select:
        return None
    selected = getattr(query, "selected", None)  # Django 6.1 and later
    if selected:
        return list(selected)
    return [*query.values_select, *query.annotation_select]


def tuple_order(queryset: "models.QuerySet[Any]", query: Any) -> tuple[int, ...] | None:
    """For `values_list()`: the order to put the columns in, if not the
    order of the `SELECT`, or `None`.

    Django 6.0 and earlier select the fields first, then the annotations, and
    their `ValuesListIterable` reorders each row to the order of the names
    given (`values_list("chapter_count", "title")`). Django 6.1 selects them
    in that order already.
    """
    fields = queryset._fields  # type: ignore[attr-defined]  # Django's: the names given
    if not fields or getattr(query, "selected", None):
        return None
    names = [*query.values_select, *query.annotation_select]
    wanted = [*fields, *(name for name in query.annotation_select if name not in fields)]
    if wanted == names:
        return None
    return tuple(names.index(name) for name in wanted)


def aggregate_builder(sql_compiler: Any) -> ResultBuilder | None:
    """A `ResultBuilder` for the one row of an `aggregate()`: one dict key per
    aggregate, converted like Django's `get_aggregation()` converts them."""
    query = sql_compiler.query
    names = list(query.annotation_select)
    expressions = list(query.annotation_select.values())
    if sql_compiler.col_count != len(names) or getattr(sql_compiler, "has_composite_fields", lambda _: False)(
        expressions
    ):
        return None
    return builder_for(names, expressions, sql_compiler)


def builder_for(
    names: list[str],
    columns: list[Any],
    sql_compiler: Any,
    model: type[models.Model] | None = None,
    related: list[RelatedModel] | None = None,
) -> ResultBuilder:
    """A `ResultBuilder` for columns named `names`, with Django's converters
    for `columns` (the selected expressions), and the ones Rust can do instead."""
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
    if model is None:
        return ResultBuilder(names, converters, conversions, sql_compiler.connection)
    return ResultBuilder(names, converters, conversions, sql_compiler.connection, model, ModelState, related or [])


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
