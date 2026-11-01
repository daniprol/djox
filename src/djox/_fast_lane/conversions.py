"""Which of Django's converters `djox.InstanceBuilder` can do in Rust.

Reading a row, Django converts some columns with its SQLite backend's
converters (text to `datetime`, a float to `Decimal`...). The fast lane hands
those columns to Rust (`src/conversions.rs`), which does the same work
without a Python call per value.

Rust may only stand in for a converter it copies exactly. So a column gets a
Rust conversion only when its converters are exactly one of Django's own,
unchanged (not a subclass's override, not a field's `from_db_value()`), and
the settings they read are the ones Rust copies. Rust keeps Django's
converter as well, for any value it doesn't handle itself.

| Django's converter (`DatabaseOperations`) | Rust conversion                         |
|-------------------------------------------|-----------------------------------------|
| `convert_datetimefield_value`             | `("datetime", timezone)`                |
| `convert_datefield_value`                 | `("date",)`                             |
| `convert_timefield_value`                 | `("time",)`                             |
| `get_decimalfield_converter()`, for a column | `("decimal", decimal_places, max_digits)` |
| `convert_booleanfield_value`              | `("boolean",)`                          |
"""

import decimal
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.db import models
from django.db.backends.sqlite3.operations import DatabaseOperations
from django.db.models.expressions import Col

Converter = Callable[[Any, Any, Any], Any]

# Django's own converters, as functions of the class, to tell them from overrides.
DJANGO_CONVERSIONS: dict[Any, str] = {
    DatabaseOperations.convert_datetimefield_value: "datetime",
    DatabaseOperations.convert_datefield_value: "date",
    DatabaseOperations.convert_timefield_value: "time",
    DatabaseOperations.convert_booleanfield_value: "boolean",
}

# The name Python gives the function `get_decimalfield_converter()` returns.
DECIMAL_CONVERTER_NAME = "DatabaseOperations.get_decimalfield_converter.<locals>.converter"


def conversion_for(converters: list[Converter], expression: Any, connection: Any) -> tuple[Any, ...] | None:
    """The Rust conversion that does what `converters` do for the column
    `expression`, or `None` to keep Django's converters (see the module docs)."""
    if len(converters) != 1:
        return None
    converter = converters[0]
    # A bound method (`connection.ops.convert_datefield_value`) remembers its function in `__func__`.
    kind = DJANGO_CONVERSIONS.get(getattr(converter, "__func__", None))
    if kind == "datetime":
        timezone = connection.timezone if settings.USE_TZ else None
        return ("datetime", timezone)
    if kind is not None:
        return (kind,)
    if is_django_decimal_converter(converter, expression, connection):
        field = expression.output_field
        return ("decimal", field.decimal_places, field.max_digits)
    return None


def is_django_decimal_converter(converter: Converter, expression: Any, connection: Any) -> bool:
    """Whether `converter` is the one Django's `get_decimalfield_converter()`
    makes for a model's column, rounding half to even like Rust does."""
    operations_class = type(connection.ops)
    if operations_class.get_decimalfield_converter is not DatabaseOperations.get_decimalfield_converter:
        return False
    # Django 6.1 makes the `Decimal` in `_create_decimal()` (earlier versions have none).
    django_create_decimal = getattr(DatabaseOperations, "_create_decimal", None)
    if getattr(operations_class, "_create_decimal", None) is not django_create_decimal:
        return False
    if getattr(converter, "__qualname__", None) != DECIMAL_CONVERTER_NAME:
        return False
    # For a column with `decimal_places`, Django rounds to them (`quantize()`).
    if not isinstance(expression, Col):
        return False
    field = expression.output_field
    if not isinstance(field, models.DecimalField) or field.decimal_places is None or field.max_digits is None:
        return False
    # Both of Django's roundings take their mode from Python's default context.
    rounds_half_to_even = decimal.DefaultContext.rounding == decimal.ROUND_HALF_EVEN
    return rounds_half_to_even and field.context.rounding == decimal.ROUND_HALF_EVEN
