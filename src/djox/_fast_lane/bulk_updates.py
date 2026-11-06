"""Updating objects in bulk on the fast lane: the `UPDATE` of each batch of `bulk_update()`.

`Book.objects.bulk_update(books, ["pages"])` updates a batch of books with
one statement:

```sql
UPDATE "library_book"
SET "pages" = CASE WHEN ("library_book"."id" = %s) THEN %s
                   WHEN ("library_book"."id" = %s) THEN %s ... ELSE NULL END
WHERE "library_book"."id" IN (%s, %s, ...)
```

Django writes it from a `Case(When(pk=..., then=Value(...)))` expression
per field, with one `When` per object. Building and compiling those takes
far longer than running the statement: about 8 ms for 100 books, which
SQLite updates in well under one. Yet the parameters are simple: for each
field, each object's primary key and value; then each primary key again.

So the fast lane lets Django write the statement once per *shape* of batch:
the model, the fields, and the type of each value, in order (which also
gives the number of objects). The type matters: a `None` is written `NULL`
rather than as a parameter, and on SQLite a `Decimal` gets another cast
than an `int`. After that, `prepare()` only computes the parameters, the
way Django's compiler does. Like for inserts (`inserts.py`), a statement is
kept only if the parameters the fast lane computes are Django's, value by
value (`learn()`).

| Left to Django                                         | Why                                   |
|--------------------------------------------------------|---------------------------------------|
| A value that is an expression (`F("pages") + 1`)       | other SQL                             |
| A field that writes its own SQL (`get_placeholder_sql`) | the SQL may change with the value    |
| Two objects with the same primary key                  | Django's `IN (...)` drops the repeat  |
| A primary key too big for its column                   | Django leaves its `When` out          |
"""

from dataclasses import dataclass
from typing import Any

from django.db import connections, models

from djox._fast_lane import is_enabled
from djox._fast_lane.parameters import same_values
from djox._fast_lane.statements import LEFT_TO_DJANGO


@dataclass(frozen=True)
class BatchUpdate:
    """One batch's `UPDATE`, prepared by the fast lane."""

    key: tuple[Any, ...]
    """The batch's shape: what decides its SQL (see the module docs)."""

    sql: str | None
    """Django's SQL for this shape, or `None` if the fast lane hasn't learned it yet."""

    parameters: list[Any]
    """The parameters, computed like Django computes them."""


# The SQL by batch shape, or `LEFT_TO_DJANGO`. A batch's SQL is long (a
# `WHEN` per object), so fewer are kept than for queries: when full, the
# cache is emptied rather than growing forever.
statements: dict[tuple[Any, ...], str] = {}
MAX_STATEMENTS = 200


def prepare(
    model: type[models.Model], alias: str, fields: list["models.Field[Any, Any]"], batch: tuple[Any, ...]
) -> BatchUpdate | None:
    """The `UPDATE` setting `fields` of the objects of `batch`, or `None` if
    Django should write it (see the module docs).

    The parameters come in Django's order: for each field, each object's
    primary key, then its value (none for a `NULL`); then the primary keys
    of the `WHERE`.
    """
    if not is_enabled():
        return None
    connection = connections[alias]
    primary_keys = primary_key_parameters(model, batch, connection)
    if primary_keys is None:
        return None
    parameters = []
    value_types = []
    for field in fields:
        if hasattr(field, "get_placeholder") or hasattr(field, "get_placeholder_sql"):
            return None
        for obj, primary_key in zip(batch, primary_keys, strict=True):
            value = getattr(obj, field.attname)
            if hasattr(value, "resolve_expression"):
                return None
            # Django's `Value(value, output_field=field)`, in an `UPDATE`, prepares it like this.
            parameter = field.get_db_prep_save(value, connection)
            parameters.append(primary_key)
            if parameter is not None:
                parameters.append(parameter)
            value_types.append((type(value), parameter is None))
    parameters.extend(primary_keys)
    key = (model, alias, tuple(field.attname for field in fields), tuple(value_types))
    sql = statements.get(key)
    if sql == LEFT_TO_DJANGO:
        return None
    return BatchUpdate(key, sql, parameters)


def primary_key_parameters(model: type[models.Model], batch: tuple[Any, ...], connection: Any) -> list[Any] | None:
    """The objects' primary keys, as Django's `pk=...` and `pk__in=[...]`
    lookups prepare them, or `None` (a repeat, a number too big for the
    column, or a value Django should refuse itself)."""
    pk_field: Any = model._meta.pk
    # A relation as primary key (a `OneToOneField`) compares the related model's key.
    lookup_field = getattr(pk_field, "target_field", None) or pk_field
    # The smallest and largest numbers of an integer column, like `(-2**63, 2**63 - 1)`, or `None`.
    integer_range = connection.ops.integer_field_ranges.get(lookup_field.get_internal_type())
    primary_keys = []
    for obj in batch:
        try:
            primary_key = lookup_field.get_db_prep_value(pk_field.get_prep_value(obj.pk), connection, prepared=True)
        except Exception:
            return None  # a value Django refuses: it raises its own error, when it builds the same lookup
        if integer_range is not None and not fits(primary_key, integer_range):
            return None
        primary_keys.append(primary_key)
    if len(set(primary_keys)) != len(primary_keys):
        return None
    return primary_keys


def fits(number: Any, integer_range: tuple[int | None, int | None]) -> bool:
    """Whether `number` fits an integer column (`None` means no limit)."""
    smallest, largest = integer_range
    return (smallest is None or number >= smallest) and (largest is None or number <= largest)


def learn(update: BatchUpdate, sql: str, django_parameters: list[Any]) -> None:
    """Keep the SQL Django wrote for `update`'s shape, if the parameters the
    fast lane computed are Django's (`django_parameters`); otherwise leave
    the shape to Django."""
    if len(statements) >= MAX_STATEMENTS:
        statements.clear()
    if same_values(list(django_parameters), update.parameters):
        statements[update.key] = sql
    else:
        statements[update.key] = LEFT_TO_DJANGO
