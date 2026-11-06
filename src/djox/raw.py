"""`Book.objects.araw()`: Django's raw SQL queries (`raw()`), run on djox.

```python
books = await Book.objects.araw("SELECT * FROM library_book WHERE pages > %s", [400])
async for book in Book.objects.araw("SELECT * FROM library_book"):
    print(book.title)
```

`raw()` runs your SQL and gives back model instances. Django's `raw()` blocks
the thread that calls it, and `async for` on its result waits on Django's
single sync thread. `araw()` runs the SQL on djox instead and awaits it, so
no thread waits.

Like the rest of djox's "safe lane", djox only runs the SQL; Django does
everything else. A `RawQuerySet` still matches the columns to the model's
fields, converts the values and builds the instances: Django's own code, fed
with the rows djox fetched.
"""

from collections.abc import Generator, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from asgiref.sync import sync_to_async
from django.db import connections, models
from django.db.models.query import RawQuerySet as DjangoRawQuerySet
from django.db.models.sql import RawQuery

from djox import _running
from djox.backends import engine
from djox.prefetch import aprefetch_related_objects

if TYPE_CHECKING:
    # `Row` exists only in the type hints of `djox._core` (`_core.pyi`).
    from djox._core import Row


class AnsweredRawQuery(RawQuery):
    """A `RawQuery` whose rows djox already fetched, so nothing runs again.

    Django's `RawQuery` runs the SQL when it is iterated, through a cursor.
    This one hands out the rows it was given instead.
    """

    def __init__(self, raw_query: RawQuery, columns: list[str], rows: "list[Row]") -> None:
        super().__init__(raw_query.sql, raw_query.using, raw_query.params)
        self.column_names = columns
        self.rows = rows

    def get_columns(self) -> list[str]:
        """The column names, like `RawQuery.get_columns()` (some databases write them in another case)."""
        converter = connections[self.using].introspection.identifier_converter
        return [converter(name) for name in self.column_names]

    def __iter__(self) -> Iterator[Any]:
        return iter(self.rows)


class RawQuerySet(DjangoRawQuerySet):  # type: ignore[type-arg]  # django-stubs makes it generic, Django's class isn't
    """Django's `RawQuerySet`, which can also be awaited or used with `async for`.

    ```python
    books = await Book.objects.araw("SELECT * FROM library_book")  # a list
    async for book in Book.objects.araw("SELECT * FROM library_book"):
        ...
    ```
    """

    def __await__(self) -> Generator[Any, None, list[Any]]:
        """`await queryset` gives the list of instances (and keeps it, like `list()` does)."""
        return self._results().__await__()

    def __aiter__(self) -> Any:
        """`async for book in queryset`: fetch all rows, then hand them out."""

        async def results() -> Any:
            for result in await self._results():
                yield result

        return results()

    async def _results(self) -> list[Any]:
        """Fetch the instances if that wasn't done yet, like Django's `_fetch_all()`."""
        if self._result_cache is None:
            await self._fetch_on_djox()
        assert self._result_cache is not None
        lookups = self._prefetch_related_lookups  # type: ignore[attr-defined]  # Django's, not in django-stubs
        if lookups and not self._prefetch_done:  # type: ignore[has-type]  # Django's
            await aprefetch_related_objects(self._result_cache, *lookups)
            self._prefetch_done = True
        return list(self._result_cache)

    async def _fetch_on_djox(self) -> None:
        """Run the SQL on djox and let Django build the instances from the rows."""
        # Named parameters (`%(name)s`, a `dict`) only work on Django's cursors.
        if not engine.alias_runs_on_djox(self.db) or isinstance(self.params, Mapping):
            self._result_cache = await sync_to_async(lambda: list(self.iterator()))()
            return
        columns, rows = await _running.fetch_all_with_columns(self.db, self.query.sql, self.parameters_for_djox())
        # Django's `iterator()` runs on a copy that has the rows: this
        # `RawQuerySet` keeps its own query, which still runs the SQL again if iterated.
        answered = self._clone()  # type: ignore[attr-defined]  # Django's, not in django-stubs
        answered.query = AnsweredRawQuery(self.query, columns, rows)
        self._result_cache = list(answered.iterator())

    def parameters_for_djox(self) -> Sequence[Any]:
        """The parameters, adapted like Django's `RawQuery` does before it runs them."""
        adapt = connections[self.db].ops.adapt_unknown_value
        return [adapt(parameter) for parameter in self.params or ()]


class AsyncRawQueries:
    """A mixin for manager classes: it adds `araw()` (the manager's `raw()` for async code)."""

    def araw(  # type: ignore[misc]  # `self` is a manager: this class is only ever mixed into one
        self: "models.Manager[Any]",
        raw_query: str,
        params: Any = (),  # a list or tuple (or a `dict` of named parameters, like `raw()` takes)
        translations: Mapping[str, str] | None = None,
        using: str | None = None,
    ) -> RawQuerySet:
        """Like Django's `raw()`, but the result can be awaited or used with `async for`, and its SQL runs on djox.

        `raw_query` uses `%s` placeholders, with `params` in a list or tuple.
        `translations` maps column names to field names, like in `raw()`.
        """
        return RawQuerySet(
            raw_query,
            model=self.model,
            params=params,
            translations=dict(translations or {}),
            using=using or self.db,
        )
