"""Which value makes each of Django's parameters.

When the fast lane learns a shape (`statements.py`), Django writes the SQL
and computes its parameters, for example for

```python
Book.objects.annotate(longer=F("pages") + 10).filter(longer__gt=500, title="Emma")
```

```sql
SELECT ..., ("pages" + %s) AS "longer" FROM "book" WHERE (("pages" + %s) > %s AND "title" = %s)
-- parameters: [10, 10, 500, "Emma"]
```

The recipe's values are `10`, `500` and `"Emma"`. The fast lane must learn
where each parameter comes from, to compute them itself for the next query
of that shape: here `(value 0, value 0, value 1, value 2)`. Django repeated
the `10`, because it wrote the annotation twice.

`parameter_sources()` finds each parameter's value by comparing them (by
value and by type: `True == 1` in Python, but they are different
parameters). The answer must be certain, or the shape isn't learned:

- **Every value must become a parameter.** A value Django wrote into the SQL
  itself (a window frame's `1 PRECEDING`) would be stale for the next query.
  `True` and `False` are the exception: they are part of the shape, so the
  SQL may hold them (Django 6.0 writes `WHERE "in_print"`).
- **A parameter that no value makes is fixed**: the same for every query of
  the shape, like the `1` that `exists()` selects, or the `'day'` of
  `TruncDay()` (`Fixed`).
- **A parameter two values could make is a doubt** (`AMBIGUOUS`): in
  `filter(start__lte=today, end__gte=today)`, both values are `today`.
  For plain filters, Django writes the values in the order of the filters,
  so the first unused one is right. Otherwise the fast lane doesn't learn
  from this query, and tries again with the next one, whose values may
  differ. So does a parameter that could be fixed: one equal to a literal of
  the shape (in `filter(kind="day")` with `TruncDay()`, is `'day'` the filter's
  value, or `TruncDay()`'s?).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from djox._fast_lane.recipe import QueryValue


@dataclass(frozen=True)
class Fixed:
    """A parameter fixed by the shape: the same for every query of the shape."""

    value: Any


# A parameter is made by the value at this position in the list of values,
# or is `Fixed`. (`int | Fixed` means "an `int` or a `Fixed`".)
Source = int | Fixed

# `parameter_sources()`'s answer when this query can't tell (see the module docs).
AMBIGUOUS = "ambiguous"


def parameter_sources(
    parameters: list[Any],
    values: tuple[QueryValue, ...],
    connection: Any,
    literals: set[Any],
) -> tuple[Source, ...] | str | None:
    """Where each of Django's `parameters` comes from: the position of the
    value that makes it, or `Fixed` (see the module docs).

    `values` are the query's values, `connection` the database's Django
    connection, `literals` the values written in the shape (names, numbers
    of slices...). Returns `AMBIGUOUS` if this query can't tell, and `None`
    if the fast lane can't compute these parameters.
    """
    computed = [value.parameter(connection) for value in values]
    all_plain = all(value.is_plain for value in values)
    sources: list[Source] = []
    used: set[int] = set()
    for parameter in parameters:
        matching = [position for position, candidate in enumerate(computed) if same_value(candidate, parameter)]
        if not matching:
            sources.append(Fixed(parameter))
            continue
        if len(matching) > 1 or could_be_fixed(parameter, literals):
            if not all_plain:
                return AMBIGUOUS
            # Plain filters: Django writes the values in their order.
            unused = [position for position in matching if position not in used]
            matching = unused or matching
        sources.append(matching[0])
        used.add(matching[0])
    for position, value in enumerate(values):
        if position not in used and not value.is_boolean:
            return None  # Django wrote this value somewhere else than the parameters
    return tuple(sources)


def could_be_fixed(parameter: Any, literals: set[Any]) -> bool:
    """Whether `parameter` is also written in the shape (see the module docs).
    `True` and `False` don't count: they are values and shape at once."""
    if isinstance(parameter, bool):
        return False
    try:
        return parameter in literals
    except TypeError:  # a value that can't be hashed (`in` needs a hash)
        return False


def literals_of(shape: Any) -> set[Any]:
    """Every text and number written in `shape` (a tuple of tuples, at any depth)."""
    literals: set[Any] = set()
    if isinstance(shape, tuple):
        for part in shape:
            literals |= literals_of(part)
    elif isinstance(shape, (str, int, float, Decimal)) and not isinstance(shape, bool):
        literals.add(shape)
    return literals


def same_values(first: list[Any], second: list[Any]) -> bool:
    """Whether two lists of parameters are equal, value by value (see `same_value()`)."""
    if len(first) != len(second):
        return False
    return all(same_value(first_value, second_value) for first_value, second_value in zip(first, second, strict=True))


def same_value(first: Any, second: Any) -> bool:
    """Whether two parameters are equal, by value and by type (`True == 1` in
    Python, but they aren't the same parameter)."""
    return type(first) is type(second) and first == second
