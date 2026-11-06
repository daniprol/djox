"""`abulk_create()`: Django's steps of `bulk_create()`, with each `INSERT` awaited on djox.

Django's `bulk_create()` is sync code that runs one `INSERT` per batch of
objects. Its async version, `abulk_create()`, runs all of it on a worker
thread. djox follows the same steps on the event loop instead:

1. Check the arguments, with Django's own checks (`_check_bulk_create_options()`).
2. Give the objects their primary key defaults and sort them: objects that
   have a primary key, and objects that get one from the database
   (Django's `_prepare_for_bulk_create()`).
3. Split each group into batches, as many objects as one statement takes
   (`connection.ops.bulk_batch_size()`).
4. For each batch, let Django's `InsertQuery` write the `INSERT`, and await
   it on djox (`compiler.run_insert()`). With more than one statement, they
   run in one `djox.atomic()` block, like Django's `atomic()`.
5. Set what the database returned (the new ids) on the objects, and mark
   them as saved (`_state`).

Each batch has its own SQL (one row of `VALUES` per object), so Django
writes it each time: the work that would be saved by keeping it is small
next to preparing each value, which Django's compiler does either way.

Models that need a query before inserting (`order_with_respect_to`) and
models with parent models (which Django refuses) run Django's
`bulk_create()` on one of djox's threads instead (`creates_on_djox()`).
"""

from collections.abc import Collection, Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, TypeVar

import django
from django.db import connections, models
from django.db.models import AutoField
from django.db.models.constants import OnConflict
from django.db.models.sql import InsertQuery

from djox import transaction as djox_transaction
from djox.backends.sqlite3 import compiler

ModelType = TypeVar("ModelType", bound=models.Model)

# Django 6.1 also sets the primary key the database returns on objects that
# already had one (the same value, converted like the database returns it).
SETS_RETURNED_PRIMARY_KEYS = django.VERSION >= (6, 1)


@dataclass(frozen=True)
class Options:
    """The options of a `bulk_create()` call, as the caller gave them."""

    batch_size: int | None
    ignore_conflicts: bool
    update_conflicts: bool
    update_fields: Collection[str] | None
    unique_fields: Collection[str] | None


@dataclass(frozen=True)
class Batch:
    """One `INSERT`: the objects and the fields it gives values to."""

    objs: list[Any]
    fields: list[Any]
    has_primary_keys: bool
    """Whether these objects had their primary key before the `INSERT`."""


def creates_on_djox(model: type[models.Model]) -> bool:
    """Whether `abulk_create()` can follow Django's steps itself for `model`
    (see the module docs for the models left to Django)."""
    meta = model._meta
    if meta.order_with_respect_to:
        return False  # Django queries the next `_order` of each group first
    # Parent models: Django refuses ("Can't bulk create a multi-table inherited model").
    return all(parent._meta.concrete_model is meta.concrete_model for parent in meta.all_parents)


async def bulk_create(queryset: "models.QuerySet[ModelType]", objs: Iterable[ModelType], options: Options) -> list[Any]:
    """Django's `bulk_create()` for `queryset`'s model, from async code (see
    the module docs). Returns the objects, with their new ids."""
    if options.batch_size is not None and options.batch_size <= 0:
        raise ValueError("Batch size must be a positive integer.")
    if not objs:
        return objs  # type: ignore[return-value]  # Django returns what it was given
    meta = queryset.model._meta
    unique_fields = None
    if options.unique_fields:
        # "pk" is allowed in `unique_fields`.
        unique_fields = [meta.get_field(meta.pk.name if name == "pk" else name) for name in options.unique_fields]
    update_fields = None
    if options.update_fields:
        update_fields = [meta.get_field(name) for name in options.update_fields]
    on_conflict = queryset._check_bulk_create_options(  # type: ignore[attr-defined]  # Django's
        options.ignore_conflicts, options.update_conflicts, update_fields, unique_fields
    )
    alias = queryset.db
    objs = list(objs)
    batches = batches_of(queryset, objs, options.batch_size)
    returning_fields = fields_to_return(queryset.model, alias, on_conflict)
    # One statement is all or nothing by itself; several share a transaction, like in Django.
    block = djox_transaction.atomic(using=alias, savepoint=False) if len(batches) > 1 else nullcontext()
    async with block:
        for batch in batches:
            query = InsertQuery(
                queryset.model, on_conflict=on_conflict, update_fields=update_fields, unique_fields=unique_fields
            )
            query.insert_values(batch.fields, batch.objs)
            insert_compiler: Any = query.get_compiler(using=alias)
            rows = await compiler.run_insert(alias, insert_compiler, returning_fields)
            mark_saved(batch, rows, returning_fields, meta.pk, alias)
    return objs


def batches_of(queryset: "models.QuerySet[Any]", objs: list[Any], batch_size: int | None) -> list[Batch]:
    """Steps 2 and 3 of the module docs: the objects with a primary key, then
    those without, each group split into batches."""
    meta = queryset.model._meta
    fields = [field for field in meta.concrete_fields if not field.generated]
    with_primary_key, without_primary_key = split_by_primary_key(queryset, objs)
    batches = []
    for group, has_primary_keys in [(with_primary_key, True), (without_primary_key, False)]:
        if not group:
            continue
        # The database gives the ids of the second group: Django leaves their column out.
        group_fields = fields if has_primary_keys else [field for field in fields if not isinstance(field, AutoField)]
        connection = connections[queryset.db]
        largest = max(connection.ops.bulk_batch_size(group_fields, group), 1)
        size = min(batch_size, largest) if batch_size else largest
        for start in range(0, len(group), size):
            batches.append(Batch(group[start : start + size], group_fields, has_primary_keys))
    return batches


def split_by_primary_key(queryset: "models.QuerySet[Any]", objs: list[Any]) -> tuple[list[Any], list[Any]]:
    """The objects that have a primary key (after their default), and those
    that don't: Django's `_prepare_for_bulk_create()`, which also checks
    their related objects are saved."""
    split = queryset._prepare_for_bulk_create(objs)  # type: ignore[attr-defined]  # Django's
    if split is not None:
        with_primary_key, without_primary_key = split
        return with_primary_key, without_primary_key
    # Django 5.2's only prepares the objects; `bulk_create()` sorts them after.
    with_primary_key = [obj for obj in objs if obj._is_pk_set()]
    without_primary_key = [obj for obj in objs if not obj._is_pk_set()]
    return with_primary_key, without_primary_key


def fields_to_return(model: type[models.Model], alias: str, on_conflict: OnConflict | None) -> Any:
    """The fields the `INSERT` returns (`RETURNING`, usually the id), or `None`.

    Not with `ignore_conflicts`: an ignored row returns nothing, so Django
    couldn't tell which object each returned row belongs to.
    """
    if not connections[alias].features.can_return_rows_from_bulk_insert:
        return None
    if on_conflict is not None and on_conflict != OnConflict.UPDATE:
        return None
    return model._meta.db_returning_fields


def mark_saved(batch: Batch, rows: list[Any], returning_fields: Any, pk_field: Any, alias: str) -> None:
    """Step 5 of the module docs: set the returned values on the objects
    (the rows come in the order of the objects), and mark them saved."""
    for obj, row in zip(batch.objs, rows, strict=False):
        for value, field in zip(row, returning_fields, strict=False):
            if batch.has_primary_keys and field == pk_field and not SETS_RETURNED_PRIMARY_KEYS:
                continue
            setattr(obj, field.attname, value)
    for obj in batch.objs:
        obj._state.adding = False
        obj._state.db = alias
