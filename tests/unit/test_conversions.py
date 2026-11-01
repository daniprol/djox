"""Rust's conversions give exactly what Django's SQLite converters give.

Each test reads values the way SQLite hands them back (text, floats,
integers) through `djox.ResultBuilder` with the conversion the fast lane
chooses, and compares with Django's own converter, value by value and type by type.
"""

import datetime
import decimal
import random
from typing import Any

import pytest
from django.db import connection, models
from django.db.models.expressions import Col
from django.test import override_settings

from djox._core import ResultBuilder
from djox._fast_lane.conversions import conversion_for


def column_of(field: "models.Field[Any, Any]") -> Col:
    """The column Django's compiler would select for `field`, as in `SELECT "price"`."""
    if not hasattr(field, "model"):  # a field made in the test, not on a model
        field.set_attributes_from_name("value")
    return Col("library_book", field)


def django_converts(field: "models.Field[Any, Any]", value: Any) -> Any:
    """What Django makes of `value`, read from `field`'s column."""
    column = column_of(field)
    for converter in connection.ops.get_db_converters(column) + field.get_db_converters(connection):
        value = converter(value, column, connection)
    return value


def rust_converts(field: "models.Field[Any, Any]", value: Any) -> Any:
    """What `ResultBuilder` makes of `value`, with the conversion the fast lane picks."""
    column = column_of(field)
    converters = connection.ops.get_db_converters(column) + field.get_db_converters(connection)
    conversion = conversion_for(converters, column, connection)
    assert conversion is not None, "the fast lane converts this field in Rust"
    builder = ResultBuilder(["value"], [(converters, column)], [conversion], connection)
    [(built,)] = builder.tuples([(value,)])
    return built


def outcome(convert: Any, field: "models.Field[Any, Any]", value: Any) -> Any:
    """What `convert` returns, or the class of the error it raises."""
    try:
        return convert(field, value)
    except Exception as error:  # compared with Django's own error below
        return type(error)


def assert_same(field: "models.Field[Any, Any]", value: Any) -> None:
    django, rust = outcome(django_converts, field, value), outcome(rust_converts, field, value)
    assert type(rust) is type(django), value
    # `str()` tells `Decimal("1.0")` from `Decimal("1.00")`, and shows the time zone.
    assert (rust, str(rust)) == (django, str(django)), value
    if isinstance(django, datetime.datetime):
        assert rust.tzinfo == django.tzinfo


PRICE = models.DecimalField(max_digits=6, decimal_places=2)  # like `Book.price`


@pytest.mark.parametrize(
    "value",
    [9.99, 10, 0, 0.0, -0.0, -1.005, 0.005, 0.015, 2.675, 9999.99, 1234.5, -0.001, None, 12, -3],
)
def test_decimals_match_django(value: Any) -> None:
    assert_same(PRICE, value)


def test_many_random_floats_round_like_django() -> None:
    numbers = random.Random(42)
    fields = [
        models.DecimalField(max_digits=places + digits, decimal_places=places)
        for places in (0, 1, 2, 5, 10)
        for digits in (1, 5, 15)
    ]
    for _ in range(20_000):
        field = numbers.choice(fields)
        # Half the floats near a tie at the field's places, like 2.675 for two places.
        scale = 10 ** numbers.randint(0, 8)
        value = numbers.uniform(-scale, scale)
        if numbers.random() < 0.5:
            value = round(value, field.decimal_places) + 5 * 10.0 ** -(field.decimal_places + 1)
        django, rust = outcome(django_converts, field, value), outcome(rust_converts, field, value)
        assert str(rust) == str(django), (value, field.max_digits, field.decimal_places)


def test_a_decimal_too_big_for_its_field_fails_like_in_django() -> None:
    assert outcome(django_converts, PRICE, 123456.0) is decimal.InvalidOperation
    assert_same(PRICE, 123456.0)


def test_decimals_stored_as_text_are_left_to_django() -> None:
    # SQLite keeps text it can't read as a number. (Django 6.0 and earlier refuse it.)
    assert_same(PRICE, "9.99")


def test_integers_with_more_than_15_digits_are_left_to_django() -> None:
    # Django 6.0 and earlier round them to 15 digits, 6.1 doesn't.
    assert_same(models.DecimalField(max_digits=30, decimal_places=2), 1234567890123456789)


@pytest.mark.parametrize(
    "value",
    ["2020-01-31 09:30:00", "2020-01-31 09:30:00.250000", "2020-01-31T09:30:00.5", "1999-12-31 23:59:59.999999", None],
)
@pytest.mark.parametrize("use_tz", [True, False])
def test_datetimes_match_django(value: Any, use_tz: bool) -> None:
    with override_settings(USE_TZ=use_tz):
        assert_same(models.DateTimeField(), value)


@pytest.mark.parametrize("value", ["2020-01-31 09:30:00+02:00", "2020-01-31 09:30", "2020-1-31 9:30:00"])
def test_unusual_datetimes_are_left_to_django(value: str) -> None:
    assert_same(models.DateTimeField(), value)


def test_an_impossible_datetime_fails_like_in_django() -> None:
    assert outcome(django_converts, models.DateTimeField(), "2020-02-30 09:30:00") is ValueError
    assert_same(models.DateTimeField(), "2020-02-30 09:30:00")


@pytest.mark.parametrize("value", ["2020-01-31", "0001-01-01", "9999-12-31", None, "2020-01-31 09:30:00"])
def test_dates_match_django(value: Any) -> None:
    assert_same(models.DateField(), value)


@pytest.mark.parametrize("value", ["09:30:00", "23:59:59.999999", "00:00:00.1", "09:30", None])
def test_times_match_django(value: Any) -> None:
    assert_same(models.TimeField(), value)


@pytest.mark.parametrize("value", [1, 0, None, 2])
def test_booleans_match_django(value: Any) -> None:
    assert_same(models.BooleanField(), value)


def test_fields_with_their_own_converters_keep_them() -> None:
    column = column_of(models.JSONField())
    converters = connection.ops.get_db_converters(column) + column.output_field.get_db_converters(connection)
    assert conversion_for(converters, column, connection) is None


def test_an_overridden_converter_is_not_replaced() -> None:
    class Operations(type(connection.ops)):  # type: ignore[misc]  # a subclass of the connection's class
        def convert_datefield_value(self, value: Any, expression: Any, connection: Any) -> Any:
            return "my date"

    operations = Operations(connection)
    column = column_of(models.DateField())
    assert conversion_for(operations.get_db_converters(column), column, connection) is None
