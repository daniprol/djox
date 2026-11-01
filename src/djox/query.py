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
  `aiterator()` streams the rows, a chunk at a time.
- **First statement**: `acount()`, `aexists()`, `acontains()`, `aupdate()`,
  `adelete()`. Run Django's sync method until it reaches its statement, await
  that on djox, and turn the answer into the result (a number, `True`...)
  in a line or two (`compiler.run_first_statement()`).
- **Replay**: `aaggregate()`. Like the above, but then Django's method runs
  again with the answer, to shape the result itself (`compiler.replay()`).
- **Create**: `acreate()`. Django's steps of `save()`, with the `INSERT`
  taken from Django's code and awaited on djox (`compiler.capture()`).

Methods that need a transaction or several statements (`aget_or_create()`,
`aupdate_or_create()`, `abulk_create()`, `abulk_update()`, cascading
`adelete()`, `ain_bulk()`, `prefetch_related()`, models with parent models)
run Django's sync method unchanged, on one of djox's own threads
(`djox._threads`): not on Django's single sync thread, where every Django
async call in the process waits its turn. Inside a transaction, they run on
the transaction's thread, so they join it.

Simple queries take the fast lane (`djox._fast_lane`): while a `QuerySet`
is only filtered with plain values (`filter(pages__gt=500)`), ordered by
columns and sliced, it keeps a note of that (`SimpleQuery`) instead of
building Django's `Query`. The reading methods (`async for`, `aget()`,
`afirst()`, `alast()`, `acount()`, `aexists()`...) then reuse the SQL Django
wrote for the same shape of query before, and build the objects in Rust. Any
other method asks for `queryset.query`, which builds Django's `Query` from
the note, so it works exactly as in Django.

When the database's `ENGINE` isn't djox's, every method is Django's own.
"""

from collections.abc import AsyncIterator, Callable, Collection, Iterable, Iterator, Mapping
from typing import Any, TypeVar

from asgiref.sync import sync_to_async
from django.db import NotSupportedError, connections, models, transaction
from django.db.models import prefetch_related_objects
from django.db.models.deletion import Collector
from django.db.models.query import (
    MAX_GET_RESULTS,  # how many rows `get()` reads at most
    FlatValuesListIterable,
    ValuesIterable,
    ValuesListIterable,
)
from django.db.models.signals import post_save, pre_save
from django.db.models.sql import Query
from django.db.models.sql.constants import MULTI

from djox import _fast_lane, _running, _threads
from djox._fast_lane import running as fast_lane
from djox._fast_lane.lookups import condition_for, is_simple_ordering, model_field, updates_for
from djox._fast_lane.running import PreparedQuery
from djox._fast_lane.simple_query import Condition, SimpleQuery, Update
from djox._fast_lane.statements import Kind
from djox._results import results_from_rows
from djox.backends import engine
from djox.backends.sqlite3 import compiler
from djox.prefetch import aprefetch_related_objects

# The model class of a `QuerySet`, like `Book` in `QuerySet[Book]`.
ModelType = TypeVar("ModelType", bound=models.Model, covariant=True)
Result = TypeVar("Result")


class QuerySet(models.QuerySet[ModelType]):
    """A Django `QuerySet` whose async methods run on djox (see the module docs).

    Use it like Django's: `djox.Manager()` on a model gives `QuerySet`s of
    this class, and a custom `QuerySet` can subclass it.
    """

    # ------------------------------------------------------ the fast lane's note

    # The note of a query simple enough for the fast lane (see the module
    # docs), or `None` once Django's `Query` is built (or the fast lane is off).
    _simple_query: SimpleQuery | None = None

    def __init__(
        self,
        model: type[models.Model] | None = None,
        query: Query | None = None,
        using: str | None = None,
        hints: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(model, query, using, hints)
        # A new QuerySet without a query means "all the objects": the simplest query there is.
        if query is None and model is not None and _fast_lane.is_enabled():
            self._simple_query = SimpleQuery()

    # `@property` makes `queryset.query` call this method, like a computed attribute.
    @property
    def query(self) -> Query:
        """Django's `Query`, built from the fast lane's note the first time anyone asks for it."""
        simple_query = self._simple_query
        if simple_query is not None:
            self._simple_query = None
            simple_query.replay_on(self)
        # Django's own property, which also applies a filter that a related manager deferred.
        return super().query

    @query.setter
    def query(self, value: Query) -> None:
        self._simple_query = None
        # Django's own setter (`super()` can't reach a property's setter).
        models.QuerySet.query.fset(self, value)  # type: ignore[attr-defined]

    def _recordable_query(self) -> SimpleQuery | None:
        """The note, if the next step can be added to it.

        Not while Django's related managers set up a queryset (they filter it
        in their own way, with `_defer_next_filter` and `_sticky_filter`).
        """
        if self._defer_next_filter or self._sticky_filter:  # type: ignore[attr-defined]  # Django's
            return None
        return self._simple_query

    def _with_simple_query(self, simple_query: SimpleQuery) -> "QuerySet[ModelType]":
        """A copy of this `QuerySet` with another note, like Django's `_clone()`,
        but without building (and copying) Django's `Query`."""
        # Django (6.1 and later) can ask a `QuerySet` to change in place
        # instead of copying itself (`_avoid_cloning()`, used by related
        # managers). A copy here would keep cloning turned off for good.
        if not getattr(self, "_cloning_enabled", True):
            self._simple_query = simple_query
            return self
        # `__new__` makes an object without calling `__init__`; then every
        # attribute is copied over, whichever ones this Django version has.
        clone = self.__class__.__new__(self.__class__)
        clone.__dict__.update(self.__dict__)
        clone._query = Query(self.model)  # type: ignore[attr-defined]  # empty, until a step needs it
        clone._result_cache = None
        clone._prefetch_done = False  # type: ignore[attr-defined]  # Django's
        clone._simple_query = simple_query
        return clone

    def filter(self, *args: Any, **kwargs: Any) -> "QuerySet[ModelType]":
        """Django's `filter()`. Plain `name=value` filters are noted for the fast lane."""
        simple_query = self._recordable_query()
        if simple_query is not None and not args and not simple_query.is_sliced:
            conditions = conditions_for(self.model, kwargs)
            if conditions is not None:
                return self._with_simple_query(simple_query.with_filter(kwargs, conditions))
        return super().filter(*args, **kwargs)

    def order_by(self, *field_names: Any) -> "QuerySet[ModelType]":
        """Django's `order_by()`. Ordering by the model's columns is noted for the fast lane."""
        simple_query = self._recordable_query()
        if simple_query is not None and not simple_query.is_sliced and is_simple_ordering(self.model, field_names):
            return self._with_simple_query(simple_query.with_order_by(field_names))
        return super().order_by(*field_names)

    def reverse(self) -> "QuerySet[ModelType]":
        """Django's `reverse()`, noted for the fast lane."""
        simple_query = self._recordable_query()
        if simple_query is not None and not simple_query.is_sliced:
            return self._with_simple_query(simple_query.with_reversed_ordering())
        return super().reverse()

    def all(self) -> "QuerySet[ModelType]":
        """Django's `all()`: a copy."""
        simple_query = self._recordable_query()
        if simple_query is not None:
            return self._with_simple_query(simple_query)
        return super().all()

    def using(self, alias: str | None) -> "QuerySet[ModelType]":
        """Django's `using()`: the same query, on another database alias."""
        simple_query = self._recordable_query()
        if simple_query is None:
            return super().using(alias)
        clone = self._with_simple_query(simple_query)
        clone._db = alias  # type: ignore[attr-defined]  # Django's
        return clone

    def select_related(self, *fields: Any) -> "QuerySet[ModelType]":
        """Django's `select_related()`: fetch the related objects in the same
        query, with a join. Relations given by name are noted for the fast lane.

        `select_related()` with no names (every non-null foreign key, which
        Django 6.1 deprecates) is Django's. So are its errors: after
        `values()` here, and a wrong name when the query runs, like in Django.
        """
        simple_query = self._recordable_query()
        if simple_query is None or self._fields is not None or not fields:  # type: ignore[attr-defined]  # Django's
            return super().select_related(*fields)
        if fields == (None,):
            return self._with_simple_query(simple_query.without_select_related())
        if not all(isinstance(name, str) for name in fields):
            return super().select_related(*fields)
        return self._with_simple_query(simple_query.with_select_related(fields))

    def prefetch_related(self, *lookups: Any) -> "QuerySet[ModelType]":
        """Django's `prefetch_related()`: after this query, fetch the related
        objects with one more query per relation (see `djox.prefetch`).

        The lookups are kept on the `QuerySet`, like Django keeps them, so a
        simple query keeps its note and still runs on the fast lane.
        """
        simple_query = self._recordable_query()
        if simple_query is None:
            return super().prefetch_related(*lookups)
        # (Django also refuses relations of `FilteredRelation`, which only
        # `annotate()` adds: a simple query has none.)
        clone = self._with_simple_query(simple_query)
        if lookups == (None,):
            clone._prefetch_related_lookups = ()  # type: ignore[attr-defined]  # Django's
        else:
            clone._prefetch_related_lookups = (*self._prefetch_lookups(), *lookups)  # type: ignore[attr-defined]
        return clone

    def values(self, *fields: Any, **expressions: Any) -> "QuerySet[Any]":
        """Django's `values()`: dicts instead of instances. The model's own fields are noted for the fast lane."""
        simple_query = self._recordable_query()
        if simple_query is None or expressions or not self._are_simple_values_fields(fields):
            return super().values(*fields, **expressions)  # type: ignore[return-value]
        return self._with_values(simple_query, fields, ValuesIterable)

    def values_list(self, *fields: Any, flat: bool = False, named: bool = False) -> "QuerySet[Any]":
        """Django's `values_list()`: tuples instead of instances (or single
        values, with `flat=True`). The model's own fields are noted for the fast lane."""
        simple_query = self._recordable_query()
        # Named tuples, and Django's errors for `flat` with other than one field, are Django's.
        if simple_query is None or named or (flat and len(fields) != 1) or not self._are_simple_values_fields(fields):
            return super().values_list(*fields, flat=flat, named=named)  # type: ignore[return-value]
        return self._with_values(simple_query, fields, FlatValuesListIterable if flat else ValuesListIterable)

    def _are_simple_values_fields(self, fields: tuple[Any, ...]) -> bool:
        """Whether `values()` or `values_list()` asks for columns of the model's
        own table only, each once (no expressions, no joins like `"author__name"`)."""
        if len(set(fields)) != len(fields):
            return False
        return all(isinstance(name, str) and model_field(self.model, name) is not None for name in fields)

    def _with_values(
        self, simple_query: SimpleQuery, fields: tuple[str, ...], iterable_class: type[Any]
    ) -> "QuerySet[Any]":
        """A copy returning `fields`, made like Django's `_values()` makes it."""
        clone = self._with_simple_query(simple_query.with_values(fields))
        clone._fields = fields  # type: ignore[attr-defined]  # Django's
        clone._iterable_class = iterable_class
        return clone

    def __getitem__(self, key: Any) -> Any:  # pyrefly: ignore[bad-override]  # one method for both of Django's overloads
        """Django's `queryset[...]`. A plain slice like `[10:20]` is noted for the fast lane."""
        simple_query = self._recordable_query()
        if (
            simple_query is not None
            and self._result_cache is None
            and isinstance(key, slice)
            and key.step is None
            and is_slice_bound(key.start)
            and is_slice_bound(key.stop)
        ):
            return self._with_simple_query(simple_query.with_limits(key.start, key.stop))
        return super().__getitem__(key)

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
        if not runs_on_djox(self.db):
            await sync_to_async(self._fetch_all)()
            return
        if self._prefetch_lookups():
            # The main query (on the fast lane when it is simple) and Django's
            # prefetching after it, together on one of djox's threads: one
            # round trip from the event loop for all of them (`djox.prefetch`).
            await self._run_django_method(self._fetch_all, writes=False)
            return
        prepared = self._prepared_for_fast_lane("rows")
        if prepared is not None:
            self._result_cache = prepared.results(await prepared.fetch_rows(), self)
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
        """Like Django's `aiterator()`: results without filling the result
        cache, read from the database `chunk_size` rows at a time, so a big
        query never holds all its rows in memory."""
        if not runs_on_djox(self.db):
            async for result in super().aiterator(chunk_size):
                yield result
            return
        if chunk_size <= 0:
            raise ValueError("Chunk size must be strictly positive.")
        async for results in self._chunks_of_results(chunk_size):
            # With `prefetch_related()`, Django prefetches for each chunk.
            lookups = self._prefetch_lookups()
            if lookups:
                await aprefetch_related_objects(results, *lookups)
            for result in results:
                yield result

    async def _chunks_of_results(self, chunk_size: int) -> AsyncIterator[list[Any]]:
        """This query's results, `chunk_size` at a time, each chunk read from
        the database when the one before has been used (`aiterator()`)."""
        prepared = self._prepared_for_fast_lane("rows")
        if prepared is not None:
            async for rows in prepared.stream_rows(chunk_size):
                yield prepared.results(rows, self)
            return
        queryset = self._chain()  # type: ignore[attr-defined]  # Django's private copy method
        sql_compiler = queryset.query.get_compiler(using=queryset.db)
        async for rows in compiler.stream_statement(queryset.db, sql_compiler, chunk_size):
            # `[rows]`: one chunk, the shape `execute_sql(MULTI)` returns.
            yield results_from_rows(queryset, sql_compiler, [rows])

    async def aget(self, *args: Any, **kwargs: Any) -> ModelType:
        """The one object matching the filters. The same steps as Django's `get()`."""
        if not runs_on_djox(self.db):
            return await super().aget(*args, **kwargs)
        clone = self._simple_queryset_for_get(args, kwargs)
        if clone is not None:
            await clone._fetch_all_on_djox()
            return self._only_result(clone._result_cache, MAX_GET_RESULTS)
        combinator = self.query.combinator
        if combinator and (args or kwargs):
            raise NotSupportedError(f"Calling QuerySet.get(...) with filters after {combinator}() is not supported.")
        clone = self._chain() if combinator else self.filter(*args, **kwargs)  # type: ignore[attr-defined]
        limit: int | None = None
        if clone.query.can_filter() and not clone.query.distinct_fields:
            clone = clone.order_by()
        select_for_update = clone.query.select_for_update
        if not select_for_update or connections[clone.db].features.supports_select_for_update_with_limit:
            limit = MAX_GET_RESULTS
            clone.query.set_limits(high=limit)
        await clone._fetch_all_on_djox()
        return self._only_result(clone._result_cache, limit)

    def _simple_queryset_for_get(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> "QuerySet[ModelType] | None":
        """What `get()` runs, on the fast lane: the same steps as Django's
        `get()` (filter, no ordering, at most `MAX_GET_RESULTS` rows), noted
        without building Django's `Query`. `None` if it isn't that simple."""
        if self._simple_query is None or args:
            return None
        clone = self.filter(**kwargs)
        simple_query = clone._simple_query
        if simple_query is None:
            return None
        if not simple_query.is_sliced:
            simple_query = simple_query.with_order_by(())
        return clone._with_simple_query(simple_query.with_limits(high=MAX_GET_RESULTS))

    def _only_result(self, results: list[Any] | None, limit: int | None) -> ModelType:
        """The one result `get()` found, or Django's error for none or several."""
        assert results is not None, "just fetched"
        if len(results) == 1:
            result: ModelType = results[0]
            return result
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
        async for result in self._first_one("first"):
            return result
        return None

    async def alast(self) -> ModelType | None:
        """The last object, or `None`. Without an ordering, ordered by primary key, like Django."""
        if not runs_on_djox(self.db):
            return await super().alast()
        async for result in self._first_one("last"):
            return result
        return None

    def _first_one(self, method: str) -> "QuerySet[ModelType]":
        """The slice `first()` (or `last()`) reads its object from: `[:1]`,
        ordered by primary key if the query has no ordering, like Django."""
        if self._keeps_its_ordering_for_first_and_last():
            queryset = self if method == "first" else self.reverse()
        else:
            if self._simple_query is None:  # (a simple query has no `GROUP BY` to check)
                self._check_ordering_first_last_queryset_aggregation(method=method)  # type: ignore[attr-defined]
            queryset = self.order_by("pk" if method == "first" else "-pk")
        first_one: QuerySet[ModelType] = queryset[:1]
        return first_one

    def _keeps_its_ordering_for_first_and_last(self) -> bool:
        """Django's test in `first()` and `last()`: the query is ordered, or was
        told not to be (`order_by()`). Otherwise they order by primary key."""
        simple_query = self._simple_query
        if simple_query is not None:
            return simple_query.order_by is not None or bool(self.model._meta.ordering)
        return self.ordered or not self.query.default_ordering

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
        if not self._can_contain(obj):
            return False
        if self._result_cache is not None:
            return obj in self._result_cache
        return await self.filter(pk=obj.pk).aexists()

    def _can_contain(self, obj: models.Model) -> bool:
        """Django's checks in `contains()`: errors for what it refuses, and
        `False` for an object of another model."""
        if self._simple_query is None:  # (a simple query is never combined)
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
        return True

    # ---------------------------------------------------------- one statement

    async def acount(self) -> int:
        """`SELECT COUNT(*)`, written by Django's `count()`."""
        if not runs_on_djox(self.db):
            return await super().acount()
        if self._result_cache is not None:
            return len(self._result_cache)
        prepared = self._prepared_for_fast_lane("count")
        if prepared is not None:
            return prepared.count(await prepared.fetch_rows())
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
        prepared = self._prepared_for_fast_lane("exists")
        if prepared is not None:
            return prepared.exists(await prepared.fetch_rows())
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
        if not runs_on_djox(using):
            return await super().aupdate(**kwargs)
        if self.model._meta.parents:
            return await self._run_django_method(lambda: self.update(**kwargs), writes=True)
        with _running.mark_for_rollback_on_error(using):
            prepared = self._prepared_update(kwargs)
            if prepared is not None:
                rows_affected = await prepared.execute()
                self._result_cache = None
                return rows_affected
        rows_affected = await compiler.run_first_statement(using, lambda: self.update(**kwargs))
        self._result_cache = None
        # No count means Django knew nothing would change (an empty filter).
        return rows_affected or 0

    async def adelete(self) -> tuple[int, dict[str, int]]:
        """`DELETE` the rows. Returns how many were deleted, in total and per model.

        djox runs the delete itself when Django would do a "fast delete": one
        `DELETE` statement, because no signal receiver and no other model's
        foreign key (`on_delete`) needs to know. Otherwise Django's `delete()`
        finds and deletes the related rows, on one of djox's threads.
        """
        if not runs_on_djox(self._alias_for_write()):
            return await super().adelete()
        prepared = self._prepared_fast_delete()
        if prepared is not None:
            with _running.mark_for_rollback_on_error(prepared.alias):
                deleted = await prepared.execute()
            return self._deleted(deleted)
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
            return await self._run_django_method(self.delete, writes=True)
        deleted = await compiler.run_first_statement(using, lambda: delete_queryset._raw_delete(using)) or 0
        return self._deleted(deleted)

    def _deleted(self, deleted: int) -> tuple[int, dict[str, int]]:
        """What `delete()` returns after a delete in one statement."""
        self._result_cache = None
        # Like Django, a model is only listed when some of its rows were deleted.
        return deleted, ({self.model._meta.label: deleted} if deleted else {})

    def _prepared_update(self, kwargs: dict[str, Any]) -> PreparedQuery | None:
        """`update(**kwargs)` on the fast lane, ready to run, or `None`.

        A sliced query is left to Django, which refuses it.
        """
        if self._simple_query is None or self._simple_query.is_sliced or self.model._meta.parents:
            return None
        updates = updates_for(self.model, kwargs)
        if updates is None:
            return None
        return self._prepared_for_fast_lane("update", updates)

    def _prepared_fast_delete(self) -> PreparedQuery | None:
        """`delete()` on the fast lane, ready to run, or `None`.

        Only when Django would delete in one statement (a "fast delete"): no
        signal receiver and no other model's foreign key needs to know.
        A sliced query is left to Django, which refuses it.
        """
        if self._simple_query is None or self._simple_query.is_sliced or self._fields is not None:  # type: ignore[attr-defined]
            return None  # Django refuses those
        using = self._alias_for_write()
        if not Collector(using=using, origin=self).can_fast_delete(self):
            return None
        return self._prepared_for_fast_lane("delete")

    # ---------------------------------------------------------------- create

    async def acreate(self, **kwargs: Any) -> ModelType:
        """Create and save an object, like Django's `create()`: the same
        checks, signals and defaults, with the `INSERT` awaited on djox."""
        meta = self.model._meta
        using = self._alias_for_write()
        # Models with parent models (multi-table inheritance) insert into
        # several tables, and `order_with_respect_to` needs a query first.
        if not runs_on_djox(using):
            return await super().acreate(**kwargs)
        if meta.parents or meta.order_with_respect_to or meta.auto_created:
            return await self._run_django_method(lambda: self.create(**kwargs), writes=True)
        reverse_one_to_one_fields = frozenset(kwargs).intersection(meta._reverse_one_to_one_field_names)  # type: ignore[attr-defined]
        if reverse_one_to_one_fields:
            raise ValueError("The following fields do not exist in this model: " + ", ".join(reverse_one_to_one_fields))
        obj = self.model(**kwargs)
        await save_new_object(obj, using)
        if hasattr(self, "_fetch_mode"):  # Django 6.1 and later
            obj._state.fetch_mode = self._fetch_mode  # pyrefly: ignore[read-only]  # django-stubs: read-only
        return obj

    # -------------------------------------------- Django's methods, on djox's threads

    async def aget_or_create(self, defaults: Mapping[str, Any] | None = None, **kwargs: Any) -> tuple[ModelType, bool]:
        """Django's `get_or_create()`: the object matching `kwargs`, or a new
        one, and whether it was created.

        The lookup comes first, with `aget()` on djox. Only when nothing
        matches does Django's `get_or_create()` run, on one of djox's threads
        (see `djox._threads`): it looks again, and creates the object, while
        no one else writes, so two tasks can't both create it.
        """
        if runs_on_djox(self.db):
            try:
                return await self.aget(**kwargs), False
            except self.model.DoesNotExist:  # type: ignore[attr-defined]  # each model class has its own
                pass
        return await self._run_django_method(lambda: self.get_or_create(defaults, **kwargs), writes=True)

    async def aupdate_or_create(
        self,
        defaults: Mapping[str, Any] | None = None,
        create_defaults: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[ModelType, bool]:
        """Django's `update_or_create()`, on one of djox's threads."""
        return await self._run_django_method(
            lambda: self.update_or_create(defaults, create_defaults, **kwargs), writes=True
        )

    async def abulk_create(
        self,
        objs: Iterable[ModelType],  # Django's name
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[ModelType]:
        """Django's `bulk_create()`, on one of djox's threads."""
        return await self._run_django_method(
            lambda: self.bulk_create(
                objs,
                batch_size=batch_size,
                ignore_conflicts=ignore_conflicts,
                update_conflicts=update_conflicts,
                update_fields=update_fields,
                unique_fields=unique_fields,
            ),
            writes=True,
        )

    async def abulk_update(
        self, objs: Iterable[ModelType], fields: Iterable[str], batch_size: int | None = None
    ) -> int:
        """Django's `bulk_update()`, on one of djox's threads."""
        return await self._run_django_method(lambda: self.bulk_update(objs, fields, batch_size), writes=True)

    async def ain_bulk(self, id_list: Iterable[Any] | None = None, *, field_name: str = "pk") -> dict[Any, ModelType]:
        """Django's `in_bulk()`, on one of djox's threads."""
        return await self._run_django_method(lambda: self.in_bulk(id_list, field_name=field_name), writes=False)

    async def aexplain(self, *, format: str | None = None, **options: Any) -> str:  # noqa: A002  (Django's name)
        """Django's `explain()`, on one of djox's threads."""
        return await self._run_django_method(lambda: self.explain(format=format, **options), writes=False)

    async def _run_django_method(self, method: Callable[[], Result], writes: bool) -> Result:
        """Run Django's sync `method` on one of djox's threads (or in the
        transaction this code is in), instead of Django's single sync thread."""
        using = self._alias_for_write() if writes else self.db
        return await _threads.run_django_method(using, method, writes)

    # ------------------------------------------------ sync methods on the fast lane

    # Django's sync methods, for simple queries: the same SQL and results as
    # Django's, without building its `Query`. Run on this thread's Django
    # connection, like Django's own (`PreparedQuery.fetch_rows_now()`).

    def _fetch_all(self) -> None:
        """Django's `_fetch_all()`, behind `for`, `list()`, `len()`, `bool()`
        and `queryset[3]`: fill the result cache, unless it is full already."""
        if self._result_cache is None:
            prepared = self._prepared_for_fast_lane("rows")
            if prepared is not None:
                self._result_cache = prepared.results(prepared.fetch_rows_now(), self)
        super()._fetch_all()  # Django's: does nothing more on a full cache

    def _iterator(self, use_chunked_fetch: bool, chunk_size: int | None) -> Iterator[ModelType]:
        """Django's generator behind `iterator()`: results without filling the
        result cache. The instances are built `chunk_size` rows at a time."""
        prepared = self._prepared_for_fast_lane("rows")
        if prepared is None:
            yield from super()._iterator(use_chunked_fetch, chunk_size)  # type: ignore[misc]  # Django's
            return
        rows = prepared.fetch_rows_now()
        lookups = self._prefetch_lookups()
        # Like Django: with `prefetch_related()`, only `iterator(chunk_size=...)` prefetches, for each chunk.
        prefetches = bool(lookups) and chunk_size is not None
        chunk_size = chunk_size or 2000
        for start in range(0, len(rows), chunk_size):
            results = prepared.results(rows[start : start + chunk_size], self)
            if prefetches:
                prefetch_related_objects(results, *lookups)
            yield from results

    def get(self, *args: Any, **kwargs: Any) -> ModelType:
        """Django's `get()`."""
        clone = self._simple_queryset_for_get(args, kwargs) if runs_on_djox(self.db) else None
        if clone is None:
            return super().get(*args, **kwargs)
        clone._fetch_all()
        return self._only_result(clone._result_cache, MAX_GET_RESULTS)

    def first(self) -> ModelType | None:
        """Django's `first()`."""
        if self._simple_query is None or not runs_on_djox(self.db):
            return super().first()
        for result in self._first_one("first"):
            return result
        return None

    def last(self) -> ModelType | None:
        """Django's `last()`."""
        if self._simple_query is None or not runs_on_djox(self.db):
            return super().last()
        for result in self._first_one("last"):
            return result
        return None

    def count(self) -> int:
        """Django's `count()`."""
        if self._result_cache is None:
            prepared = self._prepared_for_fast_lane("count")
            if prepared is not None:
                return prepared.count(prepared.fetch_rows_now())
        return super().count()

    def exists(self) -> bool:
        """Django's `exists()`."""
        if self._result_cache is None:
            prepared = self._prepared_for_fast_lane("exists")
            if prepared is not None:
                return prepared.exists(prepared.fetch_rows_now())
        return super().exists()

    def contains(self, obj: models.Model) -> bool:
        """Django's `contains()`."""
        if self._simple_query is None or not runs_on_djox(self.db):
            return super().contains(obj)
        if not self._can_contain(obj):
            return False
        if self._result_cache is not None:
            return obj in self._result_cache
        return self.filter(pk=obj.pk).exists()

    def update(self, **kwargs: Any) -> int:
        """Django's `update()`."""
        using = self._alias_for_write()
        with transaction.mark_for_rollback_on_error(using=using):
            prepared = self._prepared_update(kwargs)
            if prepared is not None:
                rows_affected = prepared.execute_now()
                self._result_cache = None
                return rows_affected
        return super().update(**kwargs)

    def delete(self) -> tuple[int, dict[str, int]]:
        """Django's `delete()`."""
        prepared = self._prepared_fast_delete()
        if prepared is None:
            return super().delete()
        # Django's `Collector` deletes inside `atomic()`, even with one statement.
        with transaction.atomic(using=prepared.alias, savepoint=False):
            deleted = prepared.execute_now()
        return self._deleted(deleted)

    # ---------------------------------------------------------------- helpers

    def _prepared_for_fast_lane(self, kind: Kind, updates: tuple[Update, ...] = ()) -> PreparedQuery | None:
        """This query, ready to run on the fast lane as `kind`, or `None` if
        it isn't simple, or its database isn't djox's."""
        if self._simple_query is None or not runs_on_djox(self.db):
            return None
        # While djox's async methods run Django's sync code to capture its SQL
        # (`compiler.capture()`), the SQL must reach Django's compiler.
        if compiler.current_replay.get() is not None:
            return None
        return fast_lane.prepare(self, kind, updates)

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


def conditions_for(model: type[models.Model], keyword_arguments: dict[str, Any]) -> list[Condition] | None:
    """The fast lane's conditions for one `filter()` call, or `None` if one of
    them must go to Django. In the order Django uses: `Q()` sorts its keyword arguments."""
    conditions = []
    for name, value in sorted(keyword_arguments.items()):
        condition = condition_for(model, name, value)
        if condition is None:
            return None
        conditions.append(condition)
    return conditions


def is_slice_bound(bound: object) -> bool:
    """Whether `bound` is a start or end of slice the fast lane notes (`None`, or an `int` from 0)."""
    return bound is None or (type(bound) is int and bound >= 0)


def runs_on_djox(alias: str) -> bool:
    """Whether the Django database `alias` uses one of djox's `ENGINE`s."""
    return engine.runs_on_djox(connections[alias])
