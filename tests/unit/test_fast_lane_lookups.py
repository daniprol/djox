"""Which `filter()` and `order_by()` arguments the fast lane takes, and how it prepares their values."""

import datetime
import decimal
import uuid
from typing import Any

import pytest
from django.db.models import Exists, F, Q, Value

from djox._fast_lane.lookups import condition_for, is_simple_ordering, where_for
from djox._fast_lane.simple_query import Where
from tests.e2e.kinds.models import EveryKind
from tests.e2e.library.models import Author, Book, Edition


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


def test_ids_from_urls_are_prepared_like_numbers() -> None:
    # `/books/7/` gives `pk="7"`: the field turns it into `7`, as Django does.
    by_text = condition_for(Book, "pk", "7")
    by_number = condition_for(Book, "pk", 7)
    assert by_text is not None
    assert by_number is not None
    assert by_text.values == by_number.values == (7,)
    by_author = condition_for(Book, "author__in", ["3", 3, "4"])
    assert by_author is not None
    assert by_author.values == (3, 4)


def test_uuids_from_urls_are_prepared_like_uuids() -> None:
    value = uuid.UUID("0b8f2c1e-6a4d-4f3b-9c55-2f1d8e7a6b10")
    condition = condition_for(EveryKind, "uuid", str(value))
    assert condition is not None
    assert condition.values == (value,)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("published__year", 2020),  # a transform
        ("author__name__icontains", "frank"),  # a lookup the fast lane doesn't know, through a join
        ("title__icontains", "dune"),  # a lookup the fast lane doesn't know
        ("pages", F("price")),  # an expression
        ("pages", Value(3)),
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


def test_a_name_follows_foreign_keys_to_the_field_it_compares() -> None:
    condition = condition_for(Edition, "book__author__name__in", ["Jane Austen"])
    assert condition is not None
    assert (condition.field, condition.lookup, condition.values) == (
        Author._meta.get_field("name"),
        "in",
        ("Jane Austen",),
    )
    on_the_relation = condition_for(Edition, "book__author__in", [1])
    assert on_the_relation is not None
    assert (on_the_relation.field, on_the_relation.lookup) == (Book._meta.get_field("author"), "in")


def test_relations_to_many_rows_go_to_django() -> None:
    assert condition_for(Book, "editions__number", 1) is None  # a reverse foreign key
    assert condition_for(Author, "books__title", "Dune") is None
    assert condition_for(Author, "biography__text", "Born") is None  # a reverse one-to-one


def test_a_filter_call_keeps_djangos_order_and_structure() -> None:
    where = where_for(Book, (Q(pages__gt=1) | ~Q(title="Dune"),), {"in_print": True, "author": 3}, negated=True)
    assert where is not None
    assert (where.connector, where.negated) == ("AND", True)
    # The `Q` first, then the keyword arguments sorted by name, like `Q(*args, **kwargs)`.
    assert [condition.name for condition in where.conditions] == ["pages__gt", "title", "author", "in_print"]
    either = where.children[0]
    assert isinstance(either, Where)
    assert (either.connector, either.negated) == ("OR", False)


@pytest.mark.parametrize(
    "q",
    [
        Q(pages__gt=1) ^ Q(title="Dune"),  # XOR
        Q(Q()),  # an empty `Q` inside
        Q(Exists(Book.objects.all())),  # an expression
        Q(title__icontains="dune") | Q(pages=1),  # one condition the fast lane doesn't know
    ],
)
def test_a_filter_call_with_anything_else_goes_to_django(q: Q) -> None:
    assert where_for(Book, (q,), {}, negated=False) is None


def test_ordering_by_columns_is_simple_and_by_anything_else_is_not() -> None:
    assert is_simple_ordering(Book, ("-pages", "pk", "author", "author_id", "author__name", "-author__pk"))
    assert not is_simple_ordering(Book, ("?",))
    assert not is_simple_ordering(Book, ("editions__number",))
    assert not is_simple_ordering(Book, ("author__name__lower",))
    assert not is_simple_ordering(Book, (F("pages").desc(),))
