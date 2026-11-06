"""Keeps a prefetching query on the fast lane, by changing one Django function.

`Author.objects.prefetch_related("books")` fetches the books with a second
query: `WHERE author_id IN (1, 2, 3...)`. Django adds that filter with
`queryset.query.add_q(...)` inside `_filter_prefetch_queryset()`. Asking a
`djox.QuerySet` for `.query` builds Django's `Query` from its recipe (see
`djox._fast_lane`), and from then on only Django can run it: the second
query would build its SQL and its model instances the slow way, every time.

This file swaps `_filter_prefetch_queryset()` for a function that, for a
`djox.QuerySet` and a plain foreign key, writes the same filter in the
recipe, like `books.filter(author__in=authors)`. Then the prefetching query
reuses the SQL Django wrote for its shape, and Rust builds the books.
Everything else (many-to-many, other kinds of querysets, a sliced queryset,
which Django filters with a window function) goes to Django's own function.

Why change a Django function, when the rest of djox subclasses? The call is
made from deep inside the related manager's `get_prefetch_querysets()`, on a
queryset it got from the related model's manager: djox can't reach it from
the `QuerySet` or the manager, and copying `get_prefetch_querysets()` would
mean following it through every Django release. The swap is checked once,
here: a Django without the function is left alone.
"""

from typing import Any

from django.db.models import ForeignKey
from django.db.models.fields import related_descriptors

# Django's function: what runs for everything djox doesn't take.
DJANGO_FILTER = getattr(related_descriptors, "_filter_prefetch_queryset", None)


def use_fast_lane_for_prefetching() -> None:
    """Swap Django's `_filter_prefetch_queryset()` for `filter_prefetch_queryset()` (once)."""
    # (A private Django function, so the type checkers don't know it.)
    if DJANGO_FILTER is None or related_descriptors._filter_prefetch_queryset is not DJANGO_FILTER:  # type: ignore[attr-defined]
        return  # a Django without the function, or already swapped
    related_descriptors._filter_prefetch_queryset = filter_prefetch_queryset  # type: ignore[attr-defined]


def filter_prefetch_queryset(queryset: Any, field_name: str, instances: Any) -> Any:
    """Django's `_filter_prefetch_queryset()`: keep only the rows related to
    `instances` through `field_name`. For a `djox.QuerySet`, it adds the filter
    to the fast lane's recipe when it can."""
    if is_plain_foreign_key(queryset, field_name):
        filter_for_prefetch = getattr(queryset, "_filter_for_prefetch", None)  # a `djox.QuerySet` has it
        if filter_for_prefetch is not None:
            filtered = filter_for_prefetch(field_name, instances)
            if filtered is not None:
                return filtered
    assert DJANGO_FILTER is not None  # (the swap only happens when it exists)
    return DJANGO_FILTER(queryset, field_name, instances)


def is_plain_foreign_key(queryset: Any, field_name: str) -> bool:
    """Whether `field_name` is a foreign key of the queryset's model made of
    one column: the filter then needs no join, so adding it with `filter()`
    gives Django's `add_q(..., reuse_all=True)`'s SQL."""
    if not hasattr(queryset, "_filter_for_prefetch"):
        return False
    field = queryset.model._meta.get_field(field_name)
    return isinstance(field, ForeignKey) and len(field.foreign_related_fields) == 1
