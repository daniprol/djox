"""`prefetch_related()` for async code: Django's own prefetching, off the event loop.

`Author.objects.prefetch_related("books")` runs two queries: the authors,
then every book of those authors at once (`WHERE author_id IN (...)`), and
hands each author its books. Django's `prefetch_related_objects()` does the
second part. It knows every kind of relation (foreign keys both ways,
many-to-many, generic relations, `Prefetch()` objects with their own
querysets, `to_attr`, lookups that go several levels deep), and each level
can only be fetched once the level before it is there.

djox runs Django's prefetching, unchanged, on one of djox's threads (see
`djox._threads`), or on the thread of the transaction this code is in, so
the prefetching queries see its writes. The event loop never waits: it
awaits the thread. `async for` on a `djox.QuerySet` with `prefetch_related()`
sends the main query there too (it still takes the fast lane when it is
simple), so the whole thing costs one round trip from the event loop.
`aiterator()` streams its rows on djox and calls `aprefetch_related_objects()`
for each chunk, like Django's.

Why not await each prefetching query on djox, like the rest of djox does?
Django's prefetching runs its queries from deep inside its own sync code
(inside the related managers, and level after level), so awaiting them means
either running that code again for each level (djox's `compiler.replay()`),
which goes wrong when a prefetched queryset adds lookups of its own, or
copying Django's prefetching into djox and following it through every
release. Both cost more than they buy: on a thread, all the queries cost one
round trip from the event loop, not one each.

Django's `aprefetch_related_objects()` does the same on Django's single sync
thread, which every async call of the process shares; djox's threads don't
wait for each other.
"""

from collections.abc import Sequence
from typing import Any

from django.db import DEFAULT_DB_ALIAS
from django.db.models import Model, prefetch_related_objects

from djox import _threads


async def aprefetch_related_objects(model_instances: Sequence[Model], *related_lookups: Any) -> None:
    """Prefetch `related_lookups` (names like `"books"`, or `Prefetch()`
    objects) on `model_instances`, like Django's `aprefetch_related_objects()`,
    on one of djox's threads (see the module docs).

    ```python
    authors = [author async for author in Author.objects.all()]
    await djox.aprefetch_related_objects(authors, "books")
    ```
    """
    if not model_instances:
        return
    # The database the instances came from decides where this runs: in its
    # open transaction, if there is one.
    # (`getattr(..., None)`: `values()` gives dicts, which Django's function skips.)
    state = getattr(model_instances[0], "_state", None)
    alias = getattr(state, "db", None) or DEFAULT_DB_ALIAS
    await _threads.run_django_method(
        alias, lambda: prefetch_related_objects(model_instances, *related_lookups), writes=False
    )
