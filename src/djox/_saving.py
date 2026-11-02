"""Saving a new object: Django's steps of `save()`, with the `INSERT` on the fast lane.

`create()`, `acreate()` and `djox.Model.asave()` of a new object come here.
For a model without parent models, Django's `save()` does:

1. check that related objects are saved (`_prepare_related_fields_for_save()`),
2. send `pre_save`,
3. give the primary key its default, compute each field's value (`auto_now`
   dates) and `INSERT` the row (`_save_table()`),
4. set the new id on the object and mark it saved (`_state`),
5. send `post_save`.

Here steps 1, 2, 4 and 5 are Django's own methods and signals, in the same
order. Step 3 is the fast lane's `INSERT` (`djox._fast_lane.inserts`) once
it knows the model's statement; until then (and for the values it leaves
to Django) it is Django's `_save_table()`, whose `INSERT` teaches the fast
lane the statement. From async code the signals are sent with `asend()`
and the `INSERT` is awaited on djox, so no thread is involved.

`object_saves_on_djox()` says which saves come here (and from async code,
`signals_can_run_here()`): the others run Django's `save()` unchanged.
With the fast lane off (`DJOX_FAST_LANE = False`), the `INSERT` is always
Django's, still awaited on djox.
"""

from collections.abc import Iterable
from typing import Any

from django.db import models, transaction
from django.db.models.signals import post_save, pre_save

from djox import _running, _threads
from djox._fast_lane import inserts
from djox.backends import engine
from djox.backends.sqlite3 import compiler


def model_saves_on_djox(model: type[models.Model], alias: str) -> bool:
    """Whether new objects of `model` can be saved here, on the database `alias`
    (see `inserts.inserts_like_django()` for the models left to Django)."""
    return (
        engine.alias_runs_on_djox(alias)
        # While djox runs Django's sync code to take its SQL (`compiler.capture()`), SQL must reach Django.
        and compiler.current_replay.get() is None
        and inserts.inserts_like_django(model)
    )


def object_saves_on_djox(
    obj: models.Model,
    alias: str,
    force_insert: object = False,
    force_update: bool = False,
    update_fields: Iterable[str] | None = None,
) -> bool:
    """Whether `obj.save(force_insert=..., force_update=..., update_fields=...,
    using=alias)` is an `INSERT` that `save_new_object()` can do, the same
    way as Django.

    `force_insert` may be a tuple of parent models in Django: those go to
    Django, like an object with deferred fields (`save()` then updates only
    the fields it has).
    """
    return (
        type(force_insert) is bool
        and not force_update
        and update_fields is None
        and model_saves_on_djox(type(obj), alias)
        and not obj.get_deferred_fields()
        and inserts.inserts_without_asking(obj, force_insert)
    )


def signals_can_run_here(model: type[models.Model]) -> bool:
    """Whether every `pre_save` and `post_save` receiver of `model` is an
    async function, which `asend()` awaits right on the event loop.

    A sync receiver may block (they often query the database), so `asend()`
    would run it on Django's sync thread, twice per save. Then the whole
    `save()` runs on one of djox's threads instead, signals included.
    """
    for signal in (pre_save, post_save):
        # `_live_receivers()` is Django's: the receivers listening now, sync ones and async ones.
        sync_receivers, _async_receivers = signal._live_receivers(model)
        if sync_receivers:
            return False
    return True


async def save_new_object(obj: models.Model, alias: str, force_insert: bool) -> None:
    """`obj.save(force_insert=force_insert, using=alias)`, from async code,
    for an object `object_saves_on_djox()` accepted (see the module docs)."""
    origin = type(obj)
    model = concrete_model(origin)
    obj._prepare_related_fields_for_save(operation_name="save")  # type: ignore[attr-defined]  # Django's
    await pre_save.asend(sender=origin, instance=obj, raw=False, using=alias, update_fields=None)
    created = True
    if inserts.inserts_without_asking(obj, force_insert):
        await insert(obj, model, alias)
    else:
        # A `pre_save` receiver gave the object a primary key: Django tries an
        # `UPDATE` first, then maybe an `INSERT`. Its own code does that, on a thread.
        updated = await _threads.run_django_method(alias, lambda: save_table(obj, model, alias), writes=True)
        created = not updated
    obj._state.db = alias
    obj._state.adding = False
    await post_save.asend(sender=origin, instance=obj, created=created, update_fields=None, raw=False, using=alias)


async def insert(obj: models.Model, model: type[models.Model], alias: str) -> None:
    """Step 3 of `save()` (see the module docs), awaited on djox."""
    # `with` around `await` is fine: the block simply lasts until the await is done.
    with _running.mark_for_rollback_on_error(alias):
        prepared = inserts.prepare(obj, model, alias)
        if prepared is not None:
            if prepared.statement.returning_fields:
                rows = await _running.fetch_all(alias, prepared.statement.sql, prepared.parameters, writes=True)
                prepared.assign_returned_values(obj, rows)
            else:
                # Nothing to read back (the object has its primary key): an `INSERT` without `RETURNING`.
                await _running.execute(alias, prepared.statement.sql, prepared.parameters)
            return
        insert_compiler = djangos_insert(obj, model, alias)
        returning_fields = insert_compiler.returning_fields
        rows = await compiler.run_insert(alias, insert_compiler, returning_fields)
        assign_returned_values(obj, rows, returning_fields)


def save_new_object_now(obj: models.Model, alias: str, force_insert: bool) -> None:
    """`obj.save(force_insert=force_insert, using=alias)`, from sync code, for
    an object `object_saves_on_djox()` accepted: like `save_new_object()`,
    on this thread's Django connection."""
    origin = type(obj)
    model = concrete_model(origin)
    obj._prepare_related_fields_for_save(operation_name="save")  # type: ignore[attr-defined]  # Django's
    pre_save.send(sender=origin, instance=obj, raw=False, using=alias, update_fields=None)
    created = True
    if inserts.inserts_without_asking(obj, force_insert):
        insert_now(obj, model, alias)
    else:
        created = not save_table(obj, model, alias)  # Django's way: see `save_new_object()`
    obj._state.db = alias
    obj._state.adding = False
    post_save.send(sender=origin, instance=obj, created=created, update_fields=None, raw=False, using=alias)


def insert_now(obj: models.Model, model: type[models.Model], alias: str) -> None:
    """Step 3 of `save()`, on this thread's Django connection."""
    with transaction.mark_for_rollback_on_error(using=alias):
        prepared = inserts.prepare(obj, model, alias)
        if prepared is not None:
            if prepared.statement.returning_fields:
                rows = _running.fetch_all_now(alias, prepared.statement.sql, prepared.parameters)
                prepared.assign_returned_values(obj, rows)
            else:
                # No `RETURNING`, so no rows: psycopg refuses to fetch them.
                _running.execute_now(alias, prepared.statement.sql, prepared.parameters)
            return
        insert_compiler = djangos_insert(obj, model, alias)
        returning_fields = insert_compiler.returning_fields
        # Django's own `execute_sql()`, on Django's connection, as in `save()`.
        rows = insert_compiler.execute_sql(returning_fields)
        assign_returned_values(obj, rows, returning_fields)


def djangos_insert(obj: models.Model, model: type[models.Model], alias: str) -> Any:
    """The `INSERT` compiler Django's `_save_table()` makes for `obj`, taken
    from it before it runs (`compiler.capture()`), and learned by the fast lane."""
    statement = compiler.capture(lambda: save_table(obj, model, alias, force_insert=True))
    insert_compiler = statement.compiler
    if not isinstance(insert_compiler, compiler.SQLInsertCompiler):
        raise RuntimeError(f"expected an INSERT from save(), got {insert_compiler!r}")
    inserts.learn(obj, model, alias, insert_compiler)
    return insert_compiler


def save_table(obj: models.Model, model: type[models.Model], alias: str, force_insert: bool = False) -> bool:
    """Django's `_save_table()` for a new object: whether it updated a row
    (rather than inserting one). Marks the transaction for rollback on an
    error, like `save_base()` does around it."""
    with transaction.mark_for_rollback_on_error(using=alias):
        updated: bool = obj._save_table(  # type: ignore[attr-defined]  # Django's
            raw=False, cls=model, force_insert=force_insert, force_update=False, using=alias, update_fields=None
        )
    return updated


def assign_returned_values(obj: models.Model, rows: list[Any], returning_fields: Any) -> None:
    """Set the values an `INSERT` returned on `obj` (its new id), like `_save_table()`."""
    if rows:
        for value, field in zip(rows[0], returning_fields or [], strict=False):
            setattr(obj, field.attname, value)


def concrete_model(model: type[models.Model]) -> type[models.Model]:
    """The model whose table holds `model`'s rows: itself, or for a proxy model, the model it proxies."""
    concrete: type[models.Model] | None = model._meta.concrete_model
    return concrete if concrete is not None else model
