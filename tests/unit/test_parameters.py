"""Finding which value makes each of Django's parameters (`parameters.py`)."""

from typing import Any

from django.db import connection, models

from djox._fast_lane.parameters import AMBIGUOUS, Fixed, parameter_sources
from djox._fast_lane.recipe import QueryValue

INTEGER = models.IntegerField()
TEXT = models.CharField()
BOOLEAN = models.BooleanField()


def plain(value: Any, field: Any = INTEGER) -> QueryValue:
    """A filter's value, which Django writes in the order of the filters."""
    return QueryValue(value, field, "lookup", is_plain=True)


def in_expression(value: Any, field: Any = INTEGER) -> QueryValue:
    """A value of an expression, which Django may write in any clause."""
    return QueryValue(value, field, "value")


def parameter(value: Any, field: Any = INTEGER) -> Any:
    """`value` as Django gives it to the database (on Postgres, `500` is `Int4(500)`)."""
    return field.get_db_prep_value(value, connection, prepared=True)


def test_parameters_in_the_order_of_the_values() -> None:
    parameters = [parameter(500), parameter("Emma", TEXT)]
    assert parameter_sources(parameters, (plain(500), plain("Emma", TEXT)), connection, set()) == (0, 1)


def test_a_value_may_make_several_parameters() -> None:
    # `annotate(longer=F("pages") + 10).filter(longer__gt=500)`: Django writes the `+ 10` twice.
    values = (in_expression(10), plain(500))
    assert parameter_sources([parameter(10), parameter(10), parameter(500)], values, connection, set()) == (0, 0, 1)


def test_a_parameter_no_value_makes_is_fixed() -> None:
    assert parameter_sources(["day", parameter(3)], (in_expression(3),), connection, set()) == (Fixed("day"), 0)


def test_equal_plain_values_go_in_order() -> None:
    # `filter(start__lte=today, end__gte=today)`.
    assert parameter_sources([parameter(7), parameter(7)], (plain(7), plain(7)), connection, set()) == (0, 1)


def test_equal_values_of_expressions_are_a_doubt() -> None:
    assert parameter_sources([parameter(7), parameter(7)], (in_expression(7), plain(7)), connection, set()) == AMBIGUOUS


def test_a_value_equal_to_a_literal_of_the_shape_is_a_doubt() -> None:
    # Is the `"day"` parameter the filter's value, or `TruncDay()`'s?
    values = (in_expression(3), plain("day", TEXT))
    assert parameter_sources(["day", parameter(3)], values, connection, {"day"}) == AMBIGUOUS


def test_types_must_match_too() -> None:
    assert parameter_sources([1], (plain(True, BOOLEAN),), connection, set()) == (Fixed(1),)


def test_a_value_written_into_the_sql_leaves_the_shape_to_django() -> None:
    assert parameter_sources([], (in_expression(2),), connection, set()) is None


def test_true_and_false_may_be_written_into_the_sql() -> None:
    # Django 6.0 writes `WHERE "in_print"` for `in_print=True`.
    assert parameter_sources([parameter(5)], (plain(True, BOOLEAN), plain(5)), connection, set()) == (1,)
