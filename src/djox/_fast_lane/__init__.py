"""The fast lane: queries skip Django's query building and compiler.

The safe lane (`djox.QuerySet` alone) lets Django build every query, write
its SQL and build its objects, and only runs the SQL on djox. That Python is
most of the time of a query. The fast lane instead writes down the calls
that make a query (a `QueryRecipe`: `filter()`, `annotate()`, `order_by()`...),
and then:

1. **reuses the SQL** Django wrote for the first query of the same *shape*
   (the same calls, with any values),
2. **computes the parameters** from the query's values, the way Django does,
3. **runs the SQL** through `djox._running`, like the safe lane,
4. **builds the results in Rust** (`ResultBuilder`: instances, dicts or
   tuples), converting dates, times, decimals and booleans in Rust the way
   Django's converters do.

It serves sync and async methods, reads (rows, `count()`, `exists()`,
`aggregate()`) and writes (`update()`, `delete()`, and the `INSERT` of
`create()` and `asave()`, with `inserts.py`).

Anything else goes to Django, at the moment Django would see it, so results
and errors are the same. `DJOX_FAST_LANE = False` in the settings turns it off.

| File             | What it does                                                          |
|------------------|-----------------------------------------------------------------------|
| `recipe.py`      | `QueryRecipe`: the calls a `QuerySet` keeps; building Django's `QuerySet` from them |
| `arguments.py`   | Reading each call's arguments into a shape and values, like Django's lookups |
| `subqueries.py`  | The recipe behind a subquery (`Exists()`, `Subquery()`, `pk__in=queryset`) |
| `checks.py`      | Django checks each new kind of call once, so its errors come at its moment |
| `statements.py`  | The SQL of each shape, written once by Django and kept                |
| `parameters.py`  | Which value makes each of Django's parameters                         |
| `results.py`     | The `ResultBuilder` of each shape                                     |
| `conversions.py` | Which of Django's converters Rust can do instead                      |
| `running.py`     | Running a recipe, sync or async, and turning its rows into results    |
| `inserts.py`     | The `INSERT` of each model's new objects, written once by Django and kept |
"""

from django.conf import settings


def is_enabled() -> bool:
    """Whether the fast lane is on (the `DJOX_FAST_LANE` setting, on by default)."""
    return bool(getattr(settings, "DJOX_FAST_LANE", True))
