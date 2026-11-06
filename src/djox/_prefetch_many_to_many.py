"""Keeps the prefetching of a many-to-many field on the fast lane.

`Shelf.objects.prefetch_related("labels")` fetches the labels of the shelves
with a second query, joined to the `through` table:

```sql
SELECT label.*, shelf_labels.shelf_id FROM label
JOIN shelf_labels ON ... WHERE shelf_labels.shelf_id IN (1, 2, 3)
```

The extra column says which shelf each label was fetched for. Django writes
the query in `get_prefetch_querysets()` of the many-to-many related manager,
and adds that column with `queryset.extra(select=...)`, which leaves the fast
lane (the column would come before the model's own, and raw SQL has no shape
to keep). So for a related model that uses djox, this file gives the manager
class a `get_prefetch_querysets()` that, when the fast lane can run the
query, notes the same thing in the recipe: a filter on the relation, and an
annotation `F("shelf")` that Django resolves to the same `through` column
(it reuses the filter's join). Anything else goes to Django's own method.

The fast lane only takes the related model's plain queryset (the manager's
`all()`, or a `Prefetch()` queryset with no calls yet). With calls before it
(`filter(...)`, `order_by(...)`), Django would reuse a join they made through
the same relation (`reuse_all=True`), where `filter()` makes a new one. Those
go to Django, which knows how to tell.

Why copy a method of Django's here? See `djox._prefetch_forward`. The manager
class is made by Django each time it is needed (`djox.related` already
changes the methods of those classes the same way).
"""

from typing import Any

from django.db.models import F

# The name Django gives the extra column (`_prefetch_related_val_shelf_id`), so the
# objects djox fetches look the same as Django's.
OWNER_COLUMN = "_prefetch_related_val_"


def use_fast_lane_for_prefetching(manager_class: type) -> None:
    """Give `manager_class`, a many-to-many related manager class made by
    Django (see `djox.related`), a `get_prefetch_querysets()` that takes the
    fast lane when it can. Other related manager classes are left alone."""
    if manager_class.__name__ != "ManyRelatedManager":  # Django's name for the many-to-many one
        return
    django_method = manager_class.__dict__["get_prefetch_querysets"]

    def get_prefetch_querysets(self: Any, instances: Any, querysets: Any = None) -> Any:
        """Django's `get_prefetch_querysets()`: the query for the objects
        related to `instances`, and how to match them (see `djox._prefetch_forward`)."""
        queryset = fast_lane_queryset(self, manager_class, instances, querysets)
        if queryset is None:
            return django_method(self, instances, querysets)
        # `through` is the table that links the two models, like `shelf_labels`;
        # `source` is its foreign key to the model of `instances`, like `shelf_id`.
        source = self.through._meta.get_field(self.source_field_name)
        owner_name = OWNER_COLUMN + source.local_related_fields[0].attname
        queryset = queryset.filter(**{f"{self.query_field_name}__in": instances})
        queryset = queryset.annotate(**{owner_name: F(self.query_field_name)})
        key_name = source.foreign_related_fields[0].attname
        return (
            queryset,
            lambda related_object: (getattr(related_object, owner_name),),  # the instance it was fetched for
            lambda instance: (getattr(instance, key_name),),
            False,  # many objects for each instance
            self.prefetch_cache_name,
            False,
        )

    manager_class.get_prefetch_querysets = get_prefetch_querysets  # type: ignore[attr-defined]


def fast_lane_queryset(manager: Any, manager_class: type, instances: Any, querysets: Any) -> Any:
    """The `djox.QuerySet` to filter for `instances`, if the fast lane can run
    the prefetching query, or `None`: then Django's method runs."""
    source = manager.through._meta.get_field(manager.source_field_name)
    # The annotation gives the `through` column only when it is the source model's primary key.
    if len(source.foreign_related_fields) != 1 or not source.foreign_related_fields[0].primary_key:
        return None
    if querysets:
        if len(querysets) != 1:
            return None  # Django's method raises its error
        queryset = querysets[0]
    else:
        queryset = super(manager_class, manager).get_queryset()  # type: ignore[arg-type]  # (the related model's manager's)
    recipe_of = getattr(queryset, "_recordable_recipe", None)  # only a `djox.QuerySet` has it
    recipe = recipe_of() if recipe_of is not None else None
    if recipe is None or recipe.steps:
        return None
    queryset._add_hints(instance=instances[0])
    return queryset.using(queryset._db or manager._db)
