"""`bulk_update()` and `abulk_update()`: Django's steps, with each batch's `UPDATE` on the fast lane.

`Book.objects.bulk_update(books, ["pages", "price"])` saves those fields
of many objects with a few statements. Django's steps, followed here:

1. Check the arguments: Django's errors, in Django's order.
2. Split the objects into batches, as many as one statement takes
   (`connection.ops.bulk_batch_size()`).
3. Run each batch's `UPDATE`: one `Case(When(pk=..., then=value))` per field,
   with one `When` per object (`djangos_update()`). With more than one
   batch, in one transaction.

Writing that `UPDATE` is most of the time Django takes, so the fast lane
keeps the SQL of each shape of batch and only computes the parameters
(`djox._fast_lane.bulk_updates`). The first batch of a shape is written by
Django, and teaches the fast lane.

From async code, every statement is awaited on djox, so no thread is
involved, and the transaction is a `djox.atomic()` block.

`updates_on_djox()` says which calls come here. The others (a `QuerySet`
with filters, whose `WHERE` the batches would share; a model with parent
models, which may update several tables) run Django's `bulk_update()`.
"""

from collections.abc import Iterable
from contextlib import nullcontext
from functools import partial
from typing import Any

from django.db import connections, models, transaction
from django.db.models import Case, Value, When
from django.db.models.functions import Cast

from djox import _running
from djox import transaction as djox_transaction
from djox._fast_lane import bulk_updates
from djox.backends import engine
from djox.backends.sqlite3 import compiler


def updates_on_djox(queryset: "models.QuerySet[Any]") -> bool:
    """Whether `queryset.bulk_update()` can follow Django's steps here: all
    the objects of a model without parent models, on one of djox's `ENGINE`s."""
    meta = queryset.model._meta
    recipe = getattr(queryset, "_recipe", None)  # djox's `QuerySet` notes its calls (`djox._fast_lane.recipe`)
    return (
        engine.alias_runs_on_djox(queryset.db)
        and recipe is not None
        and not recipe.steps
        and not meta.parents
        and len(meta.pk_fields) == 1  # not a composite primary key
        # While djox runs Django's sync code to take its SQL (`compiler.capture()`), SQL must reach Django.
        and compiler.current_replay.get() is None
    )


def bulk_update_now(
    queryset: "models.QuerySet[Any]", objs: Iterable[Any], field_names: Iterable[str], batch_size: int | None
) -> int:
    """`bulk_update()` from sync code, on this thread's Django connection.
    Returns how many rows were updated."""
    fields, batches = checked_fields_and_batches(queryset, objs, field_names, batch_size)
    alias = queryset.db
    rows_updated = 0
    block = transaction.atomic(using=alias, savepoint=False) if len(batches) > 1 else nullcontext()
    with block:
        for batch in batches:
            update = bulk_updates.prepare(queryset.model, alias, fields, batch)
            if update is None:
                rows_updated += djangos_update(queryset, fields, batch)
                continue
            sql, parameters = sql_and_parameters(queryset, fields, batch, update)
            with transaction.mark_for_rollback_on_error(using=alias):
                rows_updated += _running.execute_now(alias, sql, parameters)
    return rows_updated


async def bulk_update(
    queryset: "models.QuerySet[Any]", objs: Iterable[Any], field_names: Iterable[str], batch_size: int | None
) -> int:
    """`bulk_update()` from async code, each statement awaited on djox.
    Returns how many rows were updated."""
    fields, batches = checked_fields_and_batches(queryset, objs, field_names, batch_size)
    alias = queryset.db
    rows_updated = 0
    block = djox_transaction.atomic(using=alias, savepoint=False) if len(batches) > 1 else nullcontext()
    async with block:
        for batch in batches:
            update = bulk_updates.prepare(queryset.model, alias, fields, batch)
            if update is None:
                # Django's `update()` until its statement, which is awaited here. (`partial(f, a)`
                # is like `lambda: f(a)`, but takes the value of `a` now, not when it's called.)
                rows = await compiler.run_first_statement(alias, partial(djangos_update, queryset, fields, batch))
                rows_updated += rows or 0  # no count: Django knew nothing would change
                continue
            sql, parameters = sql_and_parameters(queryset, fields, batch, update)
            with _running.mark_for_rollback_on_error(alias):
                rows_updated += await _running.execute(alias, sql, parameters)
    return rows_updated


def checked_fields_and_batches(
    queryset: "models.QuerySet[Any]", objs: Iterable[Any], field_names: Iterable[str], batch_size: int | None
) -> tuple[list[Any], list[tuple[Any, ...]]]:
    """Steps 1 and 2 of the module docs: the fields to update, and the
    batches of objects (none, for no objects). Raises Django's errors."""
    if batch_size is not None and batch_size <= 0:
        raise ValueError("Batch size must be a positive integer.")
    if not field_names:
        raise ValueError("Field names must be given to bulk_update().")
    objs = tuple(objs)
    if not all(obj._is_pk_set() for obj in objs):
        raise ValueError("All bulk_update() objects must have a primary key set.")
    meta = queryset.model._meta
    fields: list[Any] = [meta.get_field(name) for name in field_names]
    if any(not field.concrete for field in fields):
        raise ValueError("bulk_update() can only be used with concrete fields.")
    primary_key_fields = set(meta.pk_fields)
    for parent in meta.all_parents:
        primary_key_fields.update(parent._meta.pk_fields)
    if any(field in primary_key_fields for field in fields):
        raise ValueError("bulk_update() cannot be used with primary key fields.")
    if not objs:
        return fields, []
    for obj in objs:
        obj._prepare_related_fields_for_save(operation_name="bulk_update", fields=fields)
    connection = connections[queryset.db]
    largest = connection.ops.bulk_batch_size([meta.pk, meta.pk, *fields], objs)
    size = min(batch_size, largest) if batch_size else largest
    batches = []
    for start in range(0, len(objs), size):
        batches.append(objs[start : start + size])
    return fields, batches


def sql_and_parameters(
    queryset: "models.QuerySet[Any]", fields: list[Any], batch: tuple[Any, ...], update: bulk_updates.BatchUpdate
) -> tuple[str, list[Any]]:
    """The batch's SQL and parameters: the fast lane's, once it knows the
    shape; otherwise Django's, which the fast lane learns from."""
    if update.sql is not None:
        return update.sql, update.parameters
    # Run Django's `update()` until its statement, and take it (`compiler.capture()`).
    statement = compiler.capture(lambda: djangos_update(queryset, fields, batch))
    sql, parameters = statement.compiler.as_sql()
    bulk_updates.learn(update, sql, list(parameters))
    return sql, list(parameters)


def djangos_update(queryset: "models.QuerySet[Any]", fields: list[Any], batch: tuple[Any, ...]) -> int:
    """Step 3 of the module docs, the way Django's `bulk_update()` does it:
    a `Case()` per field, then Django's `update()` on the batch's rows."""
    connection = connections[queryset.db]
    values: dict[str, Any] = {}  # each field's `Case()` (or `Cast()`)
    for field in fields:
        whens = []
        for obj in batch:
            value = getattr(obj, field.attname)
            if not hasattr(value, "resolve_expression"):
                value = Value(value, output_field=field)
            whens.append(When(pk=obj.pk, then=value))
        case = Case(*whens, output_field=field)
        if connection.features.requires_casted_case_in_updates:
            values[field.attname] = Cast(case, output_field=field)  # Postgres needs the type of the `CASE` said
        else:
            values[field.attname] = case
    rows = queryset.filter(pk__in=[obj.pk for obj in batch])
    # Django's own `update()`: djox's would try the general fast lane, which
    # can't tell apart this statement's many alike parameters.
    return models.QuerySet.update(rows, **values)
