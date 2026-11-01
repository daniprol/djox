"""`djox.QuerySet`: Django's `QuerySet`, with its async methods running on djox.

Django's async methods (`aget()`, `acount()`, `async for`...) run the sync
method on a worker thread and wait for it. This `QuerySet` keeps everything
else from Django (building queries, `filter()`, `order_by()`...), but its
async methods run the SQL on djox and await it, so no thread waits.

They follow the "safe lane": Django writes the SQL, djox only runs it. So the
SQL is exactly the SQL Django's sync methods would run, and results are built
by Django's own code. Three ways are used, depending on the method:

- **Rows**: `async for`, `aiterator()`, `aget()`, `afirst()`, `alast()`,
  `aearliest()`, `alatest()`. Compile the `SELECT`, await its rows on djox,
  and let Django's iterable class build the results (`djox._results`).
- **First statement**: `acount()`, `aexists()`, `acontains()`, `aupdate()`,
  `adelete()`. Run Django's sync method until it reaches its statement, await
  that on djox, and turn the answer into the result (a number, `True`...)
  in a line or two (`compiler.run_first_statement()`).
- **Replay**: `aaggregate()`. Like the above, but then Django's method runs
  again with the answer, to shape the result itself (`compiler.replay()`).
- **Create**: `acreate()`. Django's steps of `save()`, with the `INSERT`
  taken from Django's code and awaited on djox (`compiler.capture()`).

Everything else, and every case these don't cover yet (like
`prefetch_related()`, or a model with parent models), calls Django's own
async method, which runs the sync code on a worker thread. So nothing breaks;
it is only slower. The same happens when the database's `ENGINE` isn't djox's.
"""

from collections.abc import AsyncIterator
from typing import Any, TypeVar

from asgiref.sync import sync_to_async
from django.db import NotSupportedError, connections, models
from django.db.models.deletion import Collector
from django.db.models.query import MAX_GET_RESULTS  # how many rows `get()` reads at most
from django.db.models.signals import post_save, pre_save
from django.db.models.sql.constants import MULTI

from djox._results import results_from_rows
from djox.backends.sqlite3 import compiler
from djox.backends.sqlite3.base import DatabaseWrapper

# The model class of a `QuerySet`, like `Book` in `QuerySet[Book]`.
ModelType = TypeVar("ModelType", bound=models.Model, covariant=True)


class QuerySet(models.QuerySet[ModelType]):
    """A Django `QuerySet` whose async methods run on djox (see the module docs).

    Use it like Django's: `djox.Manager()` on a model gives `QuerySet`s of
    this class, and a custom `QuerySet` can subclass it.
    """

    # ------------------------------------------------------------- reading rows

    def __aiter__(self) -> AsyncIterator[ModelType]:
        """`async for book in Book.objects.filter(...)`: fetch all rows, then
        hand them out (and keep them, like `for` does)."""

        async def results() -> AsyncIterator[ModelType]:
            await self._fetch_all_on_djox()
            assert self._result_cache is not None
            for result in self._result_cache:
                yield result

        return results()

    async def _fetch_all_on_djox(self) -> None:
        """Fill the result cache, like Django's `_fetch_all()`, unless it is full already."""
        if self._result_cache is not None:
            return
        if self._prefetch_lookups() or not runs_on_djox(self.db):
            # `prefetch_related()` runs more queries after this one: Django's way.
            await sync_to_async(self._fetch_all)()
            return
        self._result_cache = await self._results_from_djox()

    async def _results_from_djox(self) -> list[Any]:
        """Run this query's `SELECT` on djox and build its results (model
        instances, dicts or tuples) with Django's own code."""
        queryset = self._chain()  # type: ignore[attr-defined]  # Django's private copy method
        sql_compiler = queryset.query.get_compiler(using=queryset.db)
        rows = await compiler.run_statement(queryset.db, sql_compiler, MULTI)
        return results_from_rows(queryset, sql_compiler, rows)

    async def aiterator(self, chunk_size: int = 2000) -> AsyncIterator[ModelType]:
        """Like Django's `aiterator()`, without filling the result cache. For
        now all rows are fetched at once (`chunk_size` is only checked)."""
        if self._prefetch_lookups() or not runs_on_djox(self.db):
            async for result in super().aiterator(chunk_size):
                yield result
            return
        if chunk_size <= 0:
            raise ValueError("Chunk size must be strictly positive.")
        for result in await self._results_from_djox():
            yield result

    async def aget(self, *args: Any, **kwargs: Any) -> ModelType:
        """The one object matching the filters. The same steps as Django's `get()`."""
        if not runs_on_djox(self.db):
            return await super().aget(*args, **kwargs)
        if self.query.combinator and (args or kwargs):
            raise NotSupportedError(
                f"Calling QuerySet.get(...) with filters after {self.query.combinator}() is not supported."
            )
        clone = self._chain() if self.query.combinator else self.filter(*args, **kwargs)  # type: ignore[attr-defined]
        if self.query.can_filter() and not self.query.distinct_fields:
            clone = clone.order_by()
        limit = None
        if not clone.query.select_for_update or connections[clone.db].features.supports_select_for_update_with_limit:
            limit = MAX_GET_RESULTS
            clone.query.set_limits(high=limit)
        await clone._fetch_all_on_djox()
        results = clone._result_cache
        assert results is not None, "just fetched"
        if len(results) == 1:
            return results[0]
        name = self.model._meta.object_name
        # Django's metaclass gives each model class its own `DoesNotExist` and
        # `MultipleObjectsReturned`; django-stubs only knows them on concrete models.
        if not results:
            raise self.model.DoesNotExist(f"{name} matching query does not exist.")  # type: ignore[attr-defined]
        how_many = len(results) if not limit or len(results) < limit else f"more than {limit - 1}"
        raise self.model.MultipleObjectsReturned(  # type: ignore[attr-defined]
            f"get() returned more than one {name} -- it returned {how_many}!"
        )

    async def afirst(self) -> ModelType | None:
        """The first object, or `None`. Without an ordering, ordered by primary key, like Django."""
        if not runs_on_djox(self.db):
            return await super().afirst()
        if self.ordered or not self.query.default_ordering:
            queryset = self
        else:
            self._check_ordering_first_last_queryset_aggregation(method="first")  # type: ignore[attr-defined]
            queryset = self.order_by("pk")
        async for result in queryset[:1]:
            return result
        return None

    async def alast(self) -> ModelType | None:
        """The last object, or `None`. Without an ordering, ordered by primary key, like Django."""
        if not runs_on_djox(self.db):
            return await super().alast()
        if self.ordered or not self.query.default_ordering:
            queryset = self.reverse()
        else:
            self._check_ordering_first_last_queryset_aggregation(method="last")  # type: ignore[attr-defined]
            queryset = self.order_by("-pk")
        async for result in queryset[:1]:
            return result
        return None

    async def aearliest(self, *fields: Any) -> ModelType:
        """The earliest object by `fields` (or the model's `get_latest_by`)."""
        if not runs_on_djox(self.db):
            return await super().aearliest(*fields)
        return await self._earliest_queryset(fields).aget()

    async def alatest(self, *fields: Any) -> ModelType:
        """The latest object by `fields` (or the model's `get_latest_by`)."""
        if not runs_on_djox(self.db):
            return await super().alatest(*fields)
        if self.query.is_sliced:
            raise TypeError("Cannot change a query once a slice has been taken.")
        return await self.reverse()._earliest_queryset(fields).aget()

    def _earliest_queryset(self, fields: tuple[Any, ...]) -> "QuerySet[ModelType]":
        """The `QuerySet` Django's `earliest()` runs `get()` on."""
        latest_by = self.model._meta.get_latest_by
        if fields:
            order_by = tuple(fields)
        elif isinstance(latest_by, str):
            order_by = (latest_by,)
        elif latest_by is not None:
            order_by = tuple(latest_by)
        else:
            raise ValueError(
                "earliest() and latest() require either fields as positional "
                "arguments or 'get_latest_by' in the model's Meta."
            )
        queryset: QuerySet[ModelType] = self._chain()  # type: ignore[attr-defined]
        queryset.query.set_limits(high=1)
        queryset.query.clear_ordering(force=True)
        queryset.query.add_ordering(*order_by)
        return queryset

    async def acontains(self, obj: models.Model) -> bool:
        """Whether `obj` is one of this `QuerySet`'s objects."""
        if not runs_on_djox(self.db):
            return await super().acontains(obj)
        self._not_support_combined_queries("contains")  # type: ignore[attr-defined]
        if self._fields is not None:  # type: ignore[attr-defined]
            raise TypeError("Cannot call QuerySet.contains() after .values() or .values_list().")
        try:
            if obj._meta.concrete_model != self.model._meta.concrete_model:
                return False
        except AttributeError:
            raise TypeError("'obj' must be a model instance.") from None
        if not obj._is_pk_set():  # type: ignore[attr-defined]
            raise ValueError("QuerySet.contains() cannot be used on unsaved objects.")
        if self._result_cache is not None:
            return obj in self._result_cache
        return await self.filter(pk=obj.pk).aexists()

    # ---------------------------------------------------------- one statement

    async def acount(self) -> int:
        """`SELECT COUNT(*)`, written by Django's `count()`."""
        if not runs_on_djox(self.db):
            return await super().acount()
        if self._result_cache is not None:
            return len(self._result_cache)
        row = await compiler.run_first_statement(self.db, lambda: self.query.get_count(using=self.db))
        # The count is the only column. No row means Django knew the answer
        # without asking (an empty filter): nothing matches.
        return row[0] if row else 0

    async def aexists(self) -> bool:
        """Whether there is any row, written by Django's `exists()`."""
        if not runs_on_djox(self.db):
            return await super().aexists()
        if self._result_cache is not None:
            return bool(self._result_cache)
        # Django's `exists()` selects `1` with `LIMIT 1`: any row means yes.
        row = await compiler.run_first_statement(self.db, lambda: self.query.has_results(using=self.db))
        return bool(row)

    async def aaggregate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Aggregates (`Sum`, `Avg`...) over the rows, written by Django's `aggregate()`."""
        if not runs_on_djox(self.db):
            return await super().aaggregate(*args, **kwargs)
        return await compiler.replay(self.db, lambda: self.aggregate(*args, **kwargs))

    async def aupdate(self, **kwargs: Any) -> int:
        """`UPDATE` the rows, written by Django's `update()`. Returns how many changed."""
        # A model with parent models (multi-table inheritance) may update
        # several tables, with ids chosen while compiling: Django's way.
        using = self._alias_for_write()
        if not runs_on_djox(using) or self.model._meta.parents:
            return await super().aupdate(**kwargs)
        rows_affected = await compiler.run_first_statement(using, lambda: self.update(**kwargs))
        self._result_cache = None
        # No count means Django knew nothing would change (an empty filter).
        return rows_affected or 0

    async def adelete(self) -> tuple[int, dict[str, int]]:
        """`DELETE` the rows. Returns how many were deleted, in total and per model.

        djox runs the delete itself when Django would do a "fast delete": one
        `DELETE` statement, because no signal receiver and no other model's
        foreign key (`on_delete`) needs to know. Otherwise Django's `adelete()`
        finds and deletes the related rows.
        """
        if not runs_on_djox(self._alias_for_write()):
            return await super().adelete()
        self._not_support_combined_queries("delete")  # type: ignore[attr-defined]
        if self.query.is_sliced:
            raise TypeError("Cannot use 'limit' or 'offset' with delete().")
        if self.query.distinct_fields:
            raise TypeError("Cannot call delete() after .distinct(*fields).")
        if self._fields is not None:  # type: ignore[attr-defined]
            raise TypeError("Cannot call delete() after .values() or .values_list()")
        delete_queryset = self._chain()  # type: ignore[attr-defined]
        delete_queryset._for_write = True
        delete_queryset.query.select_for_update = False
        delete_queryset.query.select_related = False
        delete_queryset.query.clear_ordering(force=True)
        using = delete_queryset.db
        collector = Collector(using=using, origin=self)
        if not collector.can_fast_delete(delete_queryset):
            return await super().adelete()
        deleted = await compiler.run_first_statement(using, lambda: delete_queryset._raw_delete(using)) or 0
        self._result_cache = None
        # Like Django, a model is only listed when some of its rows were deleted.
        return deleted, ({self.model._meta.label: deleted} if deleted else {})

    # ---------------------------------------------------------------- create

    async def acreate(self, **kwargs: Any) -> ModelType:
        """Create and save an object, like Django's `create()`: the same
        checks, signals and defaults, with the `INSERT` awaited on djox."""
        meta = self.model._meta
        using = self._alias_for_write()
        # Models with parent models (multi-table inheritance) insert into
        # several tables, and `order_with_respect_to` needs a query first.
        if not runs_on_djox(using) or meta.parents or meta.order_with_respect_to or meta.auto_created:
            return await super().acreate(**kwargs)
        reverse_one_to_one_fields = frozenset(kwargs).intersection(meta._reverse_one_to_one_field_names)  # type: ignore[attr-defined]
        if reverse_one_to_one_fields:
            raise ValueError("The following fields do not exist in this model: " + ", ".join(reverse_one_to_one_fields))
        obj = self.model(**kwargs)
        await save_new_object(obj, using)
        if hasattr(self, "_fetch_mode"):  # Django 6.1 and later
            obj._state.fetch_mode = self._fetch_mode  # pyrefly: ignore[read-only]  # django-stubs: read-only
        return obj

    # ---------------------------------------------------------------- helpers

    def _alias_for_write(self) -> str:
        """The database alias writes go to (the router's `db_for_write()`)."""
        self._for_write = True  # what Django's write methods set
        return self.db

    def _prefetch_lookups(self) -> tuple[Any, ...]:
        """The `prefetch_related()` lookups, if any."""
        return tuple(self._prefetch_related_lookups)  # type: ignore[attr-defined]


async def save_new_object(obj: models.Model, using: str) -> None:
    """Insert a new object, like `obj.save(force_insert=True, using=using)`
    does for a model without parent models.

    `Model.save()` sends `pre_save`, fills defaults (`auto_now` dates, a
    `UUID` primary key...), inserts, sets the new id on the object, and sends
    `post_save`. Here the signals are sent with `asend()`, and Django's own
    `_save_table()` fills the defaults and writes the `INSERT`, which
    `compiler.capture()` takes from it to await on djox.
    """
    model = origin = type(obj)
    if model._meta.proxy:
        model = model._meta.concrete_model  # type: ignore[assignment]
    obj._prepare_related_fields_for_save(operation_name="save")  # type: ignore[attr-defined]
    await pre_save.asend(sender=origin, instance=obj, raw=False, using=using, update_fields=None)

    def save_table() -> None:
        obj._save_table(raw=False, cls=model, force_insert=True, force_update=False, using=using, update_fields=None)  # type: ignore[attr-defined]

    statement = compiler.capture(save_table)
    insert_compiler = statement.compiler
    if not isinstance(insert_compiler, compiler.SQLInsertCompiler):
        raise RuntimeError(f"expected an INSERT from save(), got {insert_compiler!r}")
    returning_fields = insert_compiler.returning_fields
    rows = await compiler.run_insert(using, insert_compiler, returning_fields)
    if rows:
        for value, field in zip(rows[0], returning_fields or [], strict=False):
            setattr(obj, field.attname, value)
    obj._state.db = using
    obj._state.adding = False
    await post_save.asend(sender=origin, instance=obj, created=True, update_fields=None, raw=False, using=using)


def runs_on_djox(alias: str) -> bool:
    """Whether the Django database `alias` uses djox's `ENGINE`."""
    return isinstance(connections[alias], DatabaseWrapper)
