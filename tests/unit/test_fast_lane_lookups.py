"""Which `filter()` and `order_by()` arguments the fast lane takes, and how it prepares their values."""

import datetime
import decimal
from typing import Any

import pytest
from django.db.models import F, Value

from djox._fast_lane.lookups import condition_for, is_simple_ordering
from tests.e2e.library.models import Author, Book


def test_a_plain_lookup_is_understood() -> None:
    condition = condition_for(Book, "pages__gte", 100)
    assert condition is not None
    assert (condition.field.name, condition.lookup, condition.values) == ("pages", "gte", (100,))


def test_no_lookup_means_exact_and_pk_means_the_primary_key() -> None:
    condition = condition_for(Book, "pk", 7)
    assert condition is not None
    assert (condition.field, condition.lookup) == (Book._meta.pk, "exact")


def test_exact_none_means_isnull() -> None:
    condition = condition_for(Book, "title", None)
    assert condition is not None
    assert (condition.lookup, condition.values, condition.detail) == ("isnull", (), True)


def test_in_drops_none_and_repeated_values_like_django() -> None:
    condition = condition_for(Book, "pages__in", [3, None, 1, 3])
    assert condition is not None
    assert condition.values == (3, 1)


def test_values_are_prepared_by_their_field() -> None:
    published = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    condition = condition_for(Book, "published__lt", published)
    assert condition is not None
    assert condition.values == (published,)
    price = condition_for(Book, "price", decimal.Decimal("9.99"))
    assert price is not None
    assert price.values == (decimal.Decimal("9.99"),)


def test_a_foreign_key_takes_an_instance_or_its_id() -> None:
    author = Author(id=3, name="Frank Herbert")
    by_instance = condition_for(Book, "author", author)
    by_id = condition_for(Book, "author_id", 3)
    assert by_instance is not None
    assert by_id is not None
    assert by_instance.values == by_id.values == (3,)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("author__name", "Frank Herbert"),  # a join
        ("published__year", 2020),  # a transform
        ("title__icontains", "dune"),  # a lookup the fast lane doesn't know
        ("pages", F("price")),  # an expression
        ("pages", Value(3)),
        ("pages", "7"),  # Django converts it in its own way
        ("pages", 7.5),
        ("pages__gt", None),  # Django raises
        ("in_print__isnull", "yes"),  # Django raises
        ("author", Book(id=1)),  # the wrong model: Django raises
        ("author", Author(name="unsaved")),  # Django raises
        ("books", 1),  # a reverse relation
        ("no_such_field", 1),
    ],
)
def test_anything_else_goes_to_django(name: str, value: Any) -> None:
    assert condition_for(Book, name, value) is None


def test_ordering_by_columns_is_simple_and_by_anything_else_is_not() -> None:
    assert is_simple_ordering(Book, ("-pages", "pk", "author", "author_id"))
    assert not is_simple_ordering(Book, ("?",))
    assert not is_simple_ordering(Book, ("author__name",))
    assert not is_simple_ordering(Book, (F("pages").desc(),))
