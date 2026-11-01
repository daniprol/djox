"""`SimpleQuery`, the fast lane's note of a query: slices, shapes, and Django's `Query` built from it."""

import itertools
from typing import Any

import pytest
from django.db.models import Q
from django.db.models.sql import Query

from djox._fast_lane.lookups import condition_for
from djox._fast_lane.simple_query import Condition, SimpleQuery
from djox.query import QuerySet
from tests.e2e.library.models import Book

SLICE_BOUNDS = [None, 0, 2, 5]


@pytest.mark.parametrize(
    ("first", "second"),
    list(itertools.product(itertools.product(SLICE_BOUNDS, repeat=2), repeat=2)),
)
def test_a_slice_of_a_slice_ends_where_djangos_does(
    first: tuple[int | None, int | None], second: tuple[int | None, int | None]
) -> None:
    django_query = Query(Book)
    django_query.set_limits(*first)
    django_query.set_limits(*second)
    simple_query = SimpleQuery().with_limits(*first).with_limits(*second)
    assert (simple_query.low, simple_query.high) == (django_query.low_mark, django_query.high_mark)
    assert simple_query.matches_nothing == django_query.is_empty()


def test_queries_that_differ_only_in_values_have_the_same_shape() -> None:
    def pages_over(pages: int) -> SimpleQuery:
        condition = condition_for(Book, "pages__gt", pages)
        assert condition is not None
        return SimpleQuery().with_filter({"pages__gt": pages}, [condition])

    assert pages_over(500).shape == pages_over(7).shape
    assert pages_over(500).parameters == [500]
    assert pages_over(500).with_limits(0, 20).shape != pages_over(500).shape, "Django writes LIMIT into the SQL"


def test_the_number_of_in_values_is_part_of_the_shape() -> None:
    two = condition_for(Book, "pages__in", [1, 2])
    three = condition_for(Book, "pages__in", [1, 2, 3])
    assert two is not None
    assert three is not None
    assert two.shape != three.shape


def test_in_without_values_matches_nothing() -> None:
    condition = Condition(Book._meta.get_field("pages"), "in", ())
    assert SimpleQuery().with_filter({"pages__in": []}, [condition]).matches_nothing


def test_replay_builds_the_query_django_would_build() -> None:
    noted = QuerySet(Book).filter(pages__gt=100, title="Dune").order_by("-pages").reverse()[2:5]
    assert noted._simple_query is not None, "every step was simple"
    django_query = Query(Book)
    django_query.add_q(Q(pages__gt=100, title="Dune"))
    django_query.clear_ordering(force=True, clear_default=False)
    django_query.add_ordering("-pages")
    django_query.standard_ordering = False
    django_query.set_limits(2, 5)
    assert str(noted.query) == str(django_query)
    assert noted._simple_query is None, "asking for the query built it"


def test_select_related_adds_to_the_relations_given_before() -> None:
    noted = SimpleQuery().with_select_related(("author",)).with_select_related(("author", "editions"))
    assert noted.select_related == ("author", "editions"), "each relation once, in the order given"
    assert noted.shape != SimpleQuery().shape, "Django writes the joins into the SQL"
    assert noted.without_select_related().select_related == ()


def test_values_forget_select_related_like_django() -> None:
    assert SimpleQuery().with_select_related(("author",)).with_values(("title",)).select_related == ()


def test_replay_builds_djangos_select_related() -> None:
    noted: Any = QuerySet(Book).select_related("author").filter(pages__gt=100)
    assert noted._simple_query is not None, "select_related() was noted"
    django_query = Query(Book)
    django_query.add_q(Q(pages__gt=100))
    django_query.add_select_related(["author"])
    assert str(noted.query) == str(django_query)


def test_prefetch_related_keeps_the_note() -> None:
    # `Any`: django-stubs doesn't know Django's private `_prefetch_related_lookups`.
    noted: Any = QuerySet(Book).filter(pages__gt=100).prefetch_related("editions").prefetch_related("author")
    assert noted._simple_query is not None, "prefetching doesn't change the query"
    assert noted._prefetch_related_lookups == ("editions", "author")
    assert noted.prefetch_related(None)._prefetch_related_lookups == ()
