"""How the fast lane reads each call's arguments: which it takes, their shapes, and how it prepares their values."""

import datetime
import decimal
import uuid
from typing import Any

import pytest
from django.db import models
from django.db.models import Count, Exists, F, Func, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Substr
from django.db.models.lookups import GreaterThan

from djox._fast_lane import arguments, subqueries
from djox._fast_lane.recipe import Condition, QueryRecipe, Where
from tests.e2e.kinds.models import EveryKind
from tests.e2e.library.models import Author, Book, Edition


def condition_for(model: Any, name: str, value: Any) -> Condition | None:
    """The condition of `filter(name=value)` on `model`."""
    return arguments.ArgumentReader(model, {}).condition_for(name, value)


def values_of(condition: Condition | None) -> tuple[Any, ...]:
    assert condition is not None
    return tuple(value.value for value in condition.values)


def where_for(model: Any, args: tuple[Any, ...], kwargs: dict[str, Any], negated: bool = False) -> Where | None:
    step = arguments.filter_step(model, QueryRecipe(), "exclude" if negated else "filter", args, kwargs)
    return step.where if step is not None else None


def shape_and_values(model: Any, method: str, *args: Any, **kwargs: Any) -> tuple[Any, list[Any]]:
    """The shape of a call of names and expressions, and its values."""
    step = arguments.step_for(model, QueryRecipe(), method, args, kwargs)
    assert step is not None
    return step.shape, [value.value for value in step.values]


# ---------------------------------------------------------------- filters


def test_a_plain_lookup_is_understood() -> None:
    condition = condition_for(Book, "pages__gte", 100)
    assert condition is not None
    assert (condition.lookup, values_of(condition)) == ("gte", (100,))
    assert condition.values[0].field is Book._meta.get_field("pages")
    assert condition.values[0].is_plain


def test_no_lookup_means_exact_and_pk_means_the_primary_key() -> None:
    condition = condition_for(Book, "pk", 7)
    assert condition is not None
    assert (condition.values[0].field, condition.lookup) == (Book._meta.pk, "exact")


def test_exact_none_means_isnull() -> None:
    condition = condition_for(Book, "title", None)
    assert condition is not None
    assert (condition.lookup, condition.values, condition.detail) == ("isnull", (), True)


def test_in_drops_none_and_repeated_values_like_django() -> None:
    assert values_of(condition_for(Book, "pages__in", [3, None, 1, 3])) == (3, 1)


def test_values_are_prepared_by_their_field() -> None:
    published = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    assert values_of(condition_for(Book, "published__lt", published)) == (published,)
    assert values_of(condition_for(Book, "price", decimal.Decimal("9.99"))) == (decimal.Decimal("9.99"),)


def test_a_foreign_key_takes_an_instance_or_its_id() -> None:
    author = Author(id=3, name="Frank Herbert")
    assert values_of(condition_for(Book, "author", author)) == values_of(condition_for(Book, "author_id", 3)) == (3,)


def test_ids_from_urls_are_prepared_like_numbers() -> None:
    # `/books/7/` gives `pk="7"`: the field turns it into `7`, as Django does.
    assert values_of(condition_for(Book, "pk", "7")) == values_of(condition_for(Book, "pk", 7)) == (7,)
    assert values_of(condition_for(Book, "author__in", ["3", 3, "4"])) == (3, 4)


def test_uuids_from_urls_are_prepared_like_uuids() -> None:
    value = uuid.UUID("0b8f2c1e-6a4d-4f3b-9c55-2f1d8e7a6b10")
    assert values_of(condition_for(EveryKind, "uuid", str(value))) == (value,)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("published__year", 2020),  # a transform
        ("author__name__icontains", "frank"),  # a lookup the fast lane doesn't know, through a join
        ("title__icontains", "dune"),  # a lookup the fast lane doesn't know
        ("pages", Exists(models.QuerySet(Book))),  # a subquery of a plain Django queryset
        ("pages", 7.5),
        ("pages__gt", None),  # Django raises
        ("in_print__isnull", "yes"),  # Django raises
        ("author", Book(id=1)),  # the wrong model: Django raises
        ("author", Author(name="unsaved")),  # Django raises
        ("no_such_field", 1),
    ],
)
def test_anything_else_goes_to_django(name: str, value: Any) -> None:
    assert condition_for(Book, name, value) is None


def test_a_name_follows_foreign_keys_to_the_field_it_compares() -> None:
    condition = condition_for(Edition, "book__author__name__in", ["Jane Austen"])
    assert condition is not None
    assert (condition.values[0].field, condition.lookup) == (Author._meta.get_field("name"), "in")
    on_the_relation = condition_for(Edition, "book__author__in", [1])
    assert on_the_relation is not None
    assert (on_the_relation.values[0].field, on_the_relation.lookup) == (Author._meta.pk, "in")


def test_a_name_follows_relations_to_many_rows() -> None:
    assert values_of(condition_for(Book, "editions__number", 1)) == (1,)  # a reverse foreign key
    assert values_of(condition_for(Author, "books__title", "Dune")) == ("Dune",)
    assert values_of(condition_for(Author, "biography__text", "Born")) == ("Born",)  # a reverse one-to-one
    # Compared with the relation itself: the related object's primary key.
    by_book = condition_for(Author, "books", Book(id=4))
    assert by_book is not None
    assert (by_book.values[0].value, by_book.values[0].field) == (4, Book._meta.pk)


def test_an_expression_as_a_value_is_shape_and_its_values_are_values() -> None:
    condition = condition_for(Book, "pages__gt", F("price") + 2)
    assert condition is not None
    assert [value.value for value in condition.values] == [2]
    assert not condition.values[0].is_plain, "Django may write it in another clause"
    assert condition_for(Book, "pages__gt", F("price") + 3).shape == condition.shape  # type: ignore[union-attr]


def test_a_filter_on_an_annotation_is_prepared_with_its_output_field() -> None:
    integer = IntegerField()
    reader = arguments.ArgumentReader(Book, {"chapter_count": integer})
    condition = reader.condition_for("chapter_count__gt", "2")
    assert condition is not None
    assert values_of(condition) == (2,)
    assert not condition.values[0].is_plain, "Django may write it in HAVING"
    assert reader.needs_check


def test_a_filter_call_keeps_djangos_order_and_structure() -> None:
    where = where_for(Book, (Q(pages__gt=1) | ~Q(title="Dune"),), {"in_print": True, "author": 3}, negated=True)
    assert where is not None
    assert (where.connector, where.negated) == ("AND", True)
    # The `Q` first, then the keyword arguments sorted by name, like `Q(*args, **kwargs)`.
    assert [value.value for value in where.values] == [1, "Dune", 3, True]
    either = where.children[0]
    assert isinstance(either, Where)
    assert (either.connector, either.negated) == ("OR", False)


@pytest.mark.parametrize(
    "q",
    [
        Q(pages__gt=1) ^ Q(title="Dune"),  # XOR
        Q(Q()),  # an empty `Q` inside
        Q(Exists(models.QuerySet(Book))),  # a subquery of a plain Django queryset
        Q(GreaterThan(F("pages"), 1)),  # a lookup as an expression
        Q(title__icontains="dune") | Q(pages=1),  # one condition the fast lane doesn't know
    ],
)
def test_a_filter_call_with_anything_else_goes_to_django(q: Q) -> None:
    assert where_for(Book, (q,), {}) is None


# ---------------------------------------------------------------- expressions


def test_names_and_expressions_are_shape_and_numbers_are_values() -> None:
    shape, values = shape_and_values(Book, "annotate", longer=F("pages") + 10, chapters=Count("editions"))
    assert values == [10]
    assert shape == shape_and_values(Book, "annotate", longer=F("pages") + 99, chapters=Count("editions"))[0]
    assert shape != shape_and_values(Book, "annotate", longer=F("pages") - 10, chapters=Count("editions"))[0]
    assert shape != shape_and_values(Book, "annotate", chapters=Count("editions"), longer=F("pages") + 10)[0], (
        "the order of the annotations is the order of the columns"
    )


def test_a_value_keeps_its_type_in_the_shape() -> None:
    whole, _ = shape_and_values(Book, "annotate", more=F("pages") + 1)
    decimal_one, _ = shape_and_values(Book, "annotate", more=F("pages") + decimal.Decimal(1))
    assert whole != decimal_one, "Django picks another output field, so other SQL and converters"


def test_true_false_and_none_are_shape() -> None:
    true_shape, values = shape_and_values(Book, "annotate", flag=Value(True))
    assert values == []
    assert true_shape != shape_and_values(Book, "annotate", flag=Value(False))[0]
    assert true_shape != shape_and_values(Book, "annotate", flag=Value(1))[0], "True == 1 in Python, not here"


def test_numbers_among_an_expressions_arguments_are_values() -> None:
    _, values = shape_and_values(Book, "annotate", start=Substr("title", 2, 3))
    assert values == [2, 3]


def test_conditions_in_expressions_are_prepared_by_their_field() -> None:
    _, values = shape_and_values(Book, "aggregate", long=Sum("pages", filter=Q(pages__gt="300")))
    assert values == [300]


def test_ordering_takes_names_and_expressions() -> None:
    assert shape_and_values(Book, "order_by", "-pages", F("title").desc(nulls_last=True))[1] == []


@pytest.mark.parametrize(
    ("method", "args", "kwargs"),
    [
        ("annotate", (), {"found": Exists(models.QuerySet(Book))}),  # a subquery of a plain Django queryset
        ("annotate", (), {"custom": type("Custom", (Func,), {"function": "CUSTOM"})("pages")}),  # not Django's
        ("annotate", (), {"text": Value(["a list"])}),  # a value of a type Django can't pick a field for
        ("values", (F("title"),), {}),  # an expression where Django takes names
    ],
)
def test_other_arguments_go_to_django(method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    assert arguments.step_for(Book, QueryRecipe(), method, args, kwargs) is None


# ---------------------------------------------------------------- subqueries


def editions_of_the_book(number: int) -> Any:
    """The editions of the outer query's book with a number from `number` on."""
    return Edition.objects.filter(book=OuterRef("pk"), number__gte=number)


def test_a_subquery_is_shaped_by_its_recipe_and_its_values_are_values() -> None:
    shape, values = shape_and_values(Book, "annotate", reprinted=Exists(editions_of_the_book(2)))
    assert values == [2]
    assert shape == shape_and_values(Book, "annotate", reprinted=Exists(editions_of_the_book(3)))[0]
    assert shape != shape_and_values(Book, "annotate", reprinted=Subquery(editions_of_the_book(2).values("pk")[:1]))[0]


def test_a_subquerys_values_may_move_to_any_clause() -> None:
    step = arguments.filter_step(Book, QueryRecipe(), "filter", (Exists(editions_of_the_book(2)),), {})
    assert step is not None
    assert step.where is not None
    assert [(value.value, value.is_plain) for value in step.where.values] == [(2, False)]
    assert step.needs_check, "Django checks the names of OuterRef() once"


def test_exists_and_not_exists_are_filters() -> None:
    reprinted = where_for(Book, (Exists(editions_of_the_book(2)),), {})
    not_reprinted = where_for(Book, (~Exists(editions_of_the_book(2)),), {})
    assert reprinted is not None
    assert not_reprinted is not None
    assert reprinted.shape != not_reprinted.shape


def test_in_a_queryset_is_a_subquery() -> None:
    condition = condition_for(Author, "pk__in", Book.objects.filter(pages__gt="500").values("author"))
    assert condition is not None
    assert values_of(condition) == (500,), "prepared when `filter()` ran, like any filter value"
    assert not condition.matches_nothing, "only an empty list matches nothing"


def test_a_query_django_changed_forgets_its_recipe() -> None:
    editions = Edition.objects.filter(number=1)
    assert subqueries.recipe_of_query(editions.query) is not None
    # `defer()` is Django's: it clones the `Query` and changes the clone.
    assert subqueries.recipe_of_query(editions.defer("number").query) is None


@pytest.mark.parametrize(
    "subquery",
    [
        Exists(models.QuerySet(Edition).filter(book=OuterRef("pk"))),  # a plain Django queryset
        Exists(Edition.objects.filter(book=OuterRef("author__name"))),  # an outer name through a relation
        Subquery(Edition.objects.values("pk"), template="ARRAY(%(subquery)s)"),  # a template of its own
    ],
)
def test_other_subqueries_go_to_django(subquery: Any) -> None:
    assert arguments.step_for(Book, QueryRecipe(), "annotate", (), {"found": subquery}) is None


# ---------------------------------------------------------------- update()


def test_update_prepares_plain_values_for_saving_and_reads_expressions() -> None:
    step = arguments.update_step(Book, {"pages": 300, "price": F("price") * 2, "title": None})
    assert step is not None
    assert [(value.value, value.prepared_as) for value in step.values] == [(300, "save"), (2, "save")]
    assert step.values[0].is_plain
    assert arguments.update_step(Book, {"author": Author(id=1)}) is None, "Django sets an instance's key its own way"
    assert arguments.update_step(Book, {"pk": 1}) is None
