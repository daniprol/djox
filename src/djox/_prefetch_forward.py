"""Keeps the prefetching of a forward foreign key on the fast lane.

`Book.objects.prefetch_related("author")` fetches the authors of the books
with a second query: `WHERE id IN (1, 2, 3...)`. Django's descriptor of a
foreign key (`ForwardManyToOneDescriptor`) writes that query in
`get_prefetch_querysets()`, with three calls that each leave the fast lane:
it asks the queryset for its `query` to resolve a name, filters with a
`TupleIn` expression, and clears the ordering on `queryset.query`. Asking
for `query` builds Django's `Query` from the recipe, and from then on only
Django can run it (see `djox._fast_lane`).

This file swaps `get_prefetch_querysets()` for a function that, when the
fast lane can run the query, writes the same filter with plain `filter()` and
`order_by()`, so the second query reuses the SQL Django wrote for its shape
and Rust builds the authors. Anything else goes to Django's own method:
other kinds of querysets, a one-to-one, a foreign key made of several
columns, a sliced or `distinct()` queryset.

Why copy a method of Django's here? It only reaches the queryset through
calls that need Django's `Query`, so there is no smaller seam (compare
`djox._prefetch_filter`, which swaps a helper function). The copy is short,
and its answers are compared with Django's by the tests, on every Django
version djox supports.

The queryset comes from the related model's *base manager* (Django's rule:
a manager that hides rows, like a soft-delete one, must not hide an author
from the book that points to them). For a model that uses djox, that
manager is Django's plain one, so this file makes a `djox.QuerySet` that
gives the same rows (`base_queryset_on_djox()`).
"""

from typing import Any

from django.db.models.fields.related_descriptors import ForwardManyToOneDescriptor

from djox.related import RunRelatedManagersOnDjox

# Django's method: what runs for everything djox doesn't take.
DJANGO_METHOD = ForwardManyToOneDescriptor.get_prefetch_querysets


def use_fast_lane_for_forward_foreign_keys() -> None:
    """Swap Django's `get_prefetch_querysets()` of foreign keys for ours (once)."""
    if ForwardManyToOneDescriptor.get_prefetch_querysets is not DJANGO_METHOD:
        return  # already swapped
    ForwardManyToOneDescriptor.get_prefetch_querysets = get_prefetch_querysets  # type: ignore[method-assign]


def get_prefetch_querysets(self: Any, instances: Any, querysets: Any = None) -> Any:
    """Django's `get_prefetch_querysets()`: the query for the objects the
    `instances` point to, and how to match them (a tuple Django's
    `prefetch_related_objects()` unpacks). `self` is the descriptor (`Book.author`)."""
    queryset = fast_lane_queryset(self, instances, querysets)
    if queryset is None:
        return DJANGO_METHOD(self, instances, querysets)
    field = self.field  # the foreign key, like `Book.author`
    # The key of each instance: `(author_id,)`. Some may have none (a nullable key).
    keys: dict[Any, None] = {}  # a dict keeps the order and drops repeats, like an ordered set
    for instance in instances:
        key = field.get_local_related_value(instance)[0]
        if key is not None:
            keys[key] = None
    # `.order_by()` with no names: each object is wanted once, so ordering them is wasted work.
    queryset = queryset.filter(**{f"{field.foreign_related_fields[0].name}__in": list(keys)}).order_by()
    return (
        queryset,
        field.get_foreign_related_value,  # the key of a fetched author: `(id,)`
        field.get_local_related_value,
        True,  # one object for each instance
        field.cache_name,
        False,
    )


def fast_lane_queryset(descriptor: Any, instances: Any, querysets: Any) -> Any:
    """The `djox.QuerySet` to filter for `instances`, if the fast lane can run
    the prefetching query, or `None`: then Django's method runs."""
    field = descriptor.field
    # A one-to-one needs the way back cached on each object; several columns need a tuple filter.
    if not field.remote_field.multiple or len(field.foreign_related_fields) != 1:
        return None
    if querysets:
        if len(querysets) != 1:
            return None  # Django's method raises its error
        queryset = querysets[0]
        queryset._add_hints(instance=instances[0])
    else:
        queryset = base_queryset_on_djox(descriptor, instances[0])
    recipe_of = getattr(queryset, "_recordable_recipe", None)  # only a `djox.QuerySet` has it
    recipe = recipe_of() if recipe_of is not None else None
    # `distinct(names)` keeps its ordering: Django doesn't clear it there, and `order_by()` would.
    if recipe is None or recipe.is_sliced or recipe.has_step("distinct"):
        return None
    return queryset


def base_queryset_on_djox(descriptor: Any, instance: Any) -> Any:
    """A `djox.QuerySet` that gives the rows Django's base manager gives for
    the related model, or `None`: for a model that doesn't use djox, or has
    a base manager of its own, Django's method runs."""
    # (Here, not at the top: `djox.query` imports this file.)
    from djox.query import QuerySet

    model = descriptor.field.remote_field.model
    if not isinstance(model._default_manager, RunRelatedManagersOnDjox) or not model._base_manager.auto_created:
        return None
    base_queryset = descriptor.get_queryset(instance=instance)  # routed to its database, with Django 6.1's fetch mode
    queryset: QuerySet[Any] = QuerySet(model, using=base_queryset._db, hints=base_queryset._hints)
    if hasattr(base_queryset, "_fetch_mode"):  # Django 6.1 and later
        queryset._fetch_mode = base_queryset._fetch_mode  # type: ignore[attr-defined]
    return queryset
