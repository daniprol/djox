"""Finding the recipe behind a subquery, so its shape and values are known.

A subquery is a query inside another one:

```python
editions = Edition.objects.filter(book=OuterRef("pk"), number__gt=1)
Book.objects.filter(Exists(editions))  # EXISTS (SELECT ...)
Book.objects.annotate(latest=Subquery(editions.values("number")[:1]))
Author.objects.filter(pk__in=Book.objects.filter(pages__gt=500).values("author"))
```

The inner `QuerySet` is a `djox.QuerySet` with its own recipe, so the
subquery's shape is the inner recipe's shape, and its values are the inner
recipe's values. `arguments.py` reads them from the recipe this file finds.

**`__in=queryset`** is easy: the queryset still has its recipe (`_recipe`),
or, once Django built its `Query`, the note on it described below.

**`Exists()` and `Subquery()`** are harder. Django's `Subquery(queryset)`
keeps no queryset, only a copy of its `Query` (`queryset.query.clone()`),
made as soon as it is created. So when djox builds a `Query` from a recipe
(`djox.QuerySet.query`), it writes the recipe on the `Query`
(`remember_recipe()`), and `Query.clone()` copies it with everything else.
`Exists()` changes its copy (`query.exists()`), but always in the same way,
so the class (`Exists` or `Subquery`) is part of the shape.

The note must never describe a `Query` it doesn't match. Django's methods
(`defer()`, `extra()`, a filter the fast lane leaves to Django...) clone the
`Query` and change the clone, then hand it to a new `QuerySet`: djox's
`QuerySet` forgets the note there (`forget_recipe()`), so the change is
never mistaken for the recipe. The other places where Django changes a
`Query` in place work on querysets of their own (prefetching, `delete()`),
which never become subqueries. A program that changes `queryset.query`
itself (`queryset.query.group_by = ...`, a private API) should not then
pass the queryset to `Exists()`.
"""

from typing import Any

from django.db.models import Exists, Subquery
from django.db.models.sql import Query

from djox._fast_lane.recipe import QueryRecipe

# The name of the note on a `Query` (an attribute Django doesn't use).
RECIPE_NOTE = "_djox_recipe"


def remember_recipe(query: Query, recipe: QueryRecipe) -> None:
    """Note on `query` that it was built from `recipe` (see the module docs)."""
    # `__dict__` holds an object's attributes: `Query.clone()` copies it.
    query.__dict__[RECIPE_NOTE] = recipe


def forget_recipe(query: Query) -> None:
    """Remove the note from `query`: Django is about to change it (see the module docs)."""
    query.__dict__.pop(RECIPE_NOTE, None)


def recipe_of_queryset(queryset: Any) -> QueryRecipe | None:
    """The recipe of a `djox.QuerySet` used as a subquery (`pk__in=queryset`),
    or `None` if it has none (a plain Django `QuerySet`, or one Django built)."""
    recipe = getattr(queryset, "_recipe", None)
    if recipe is not None:
        return recipe  # type: ignore[no-any-return]  # djox.QuerySet's
    if getattr(queryset, "_deferred_filter", None):
        return None  # Django changes the `Query` before using it (a related manager's filter)
    query = getattr(queryset, "_query", None)  # Django's, already built
    if query is None:
        return None
    return recipe_of_query(query)


def recipe_of_subquery(subquery: Subquery) -> QueryRecipe | None:
    """The recipe of `Exists(queryset)` or `Subquery(queryset)`, or `None` if
    it has none, or if the subquery may write SQL of its own: a class of the
    program's own, or a `template` or extra arguments given to it."""
    if type(subquery) not in (Exists, Subquery):
        return None
    if subquery.extra or subquery.template != type(subquery).template:
        return None
    return recipe_of_query(subquery.query)


def recipe_of_query(query: Query) -> QueryRecipe | None:
    """The recipe noted on `query`, if any."""
    recipe = query.__dict__.get(RECIPE_NOTE)
    return recipe if isinstance(recipe, QueryRecipe) else None
