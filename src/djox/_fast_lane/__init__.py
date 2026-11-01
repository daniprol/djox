"""The fast lane: simple queries skip Django's query building and compiler.

The safe lane (`djox.QuerySet` alone) lets Django build every query, write
its SQL and build its objects, and only runs the SQL on djox. That Python is
most of the time of a query. The fast lane notices queries that are simple
enough, writes down what they ask for (`SimpleQuery`), and then:

1. **reuses the SQL** Django wrote for the first query of the same *shape*
   (the same filters, ordering and slice, with any values),
2. **computes the parameters** with the fields' own methods, like Django's lookups,
3. **runs the SQL** through `djox._running`, like the safe lane,
4. **builds the instances in Rust** (`djox.InstanceBuilder`), converting dates,
   decimals and booleans in Rust the way Django's converters do.

Anything else goes to Django, at the moment Django would see it, so results
and errors are the same. `DJOX_FAST_LANE = False` in the settings turns it off.

| File              | What it does                                                          |
|-------------------|-----------------------------------------------------------------------|
| `simple_query.py` | `SimpleQuery`: the note a `QuerySet` keeps; building Django's `Query` from it |
| `lookups.py`      | Understanding `filter()` and `order_by()` arguments, like Django's lookups |
| `statements.py`   | The SQL of each shape, written once by Django and kept                |
| `conversions.py`  | Which of Django's converters Rust can do instead                      |
| `running.py`      | Running a simple query: `fetch_results()`, `count()`, `exists()`      |
"""

from django.conf import settings


def is_enabled() -> bool:
    """Whether the fast lane is on (the `DJOX_FAST_LANE` setting, on by default)."""
    return bool(getattr(settings, "DJOX_FAST_LANE", True))
