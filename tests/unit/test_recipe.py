"""`QueryRecipe`, the calls a `QuerySet` keeps: slices, shapes, and Django's `Query` built from them."""

import itertools
from typing import Any

import pytest
from django.db import models
from django.db.models import Count, F, Q
from django.db.models.sql import Query

from djox._fast_lane import arguments
from djox._fast_lane.recipe import QueryRecipe
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
    recipe = QueryRecipe().with_step(arguments.slice_step(*first)).with_step(arguments.slice_step(*second))
    assert (recipe.low, recipe.high) == (django_query.low_mark, django_query.high_mark)
    assert recipe.matches_nothing == django_query.is_empty()


def filtered(*args: Any, negated: bool = False, **kwargs: Any) -> QueryRecipe:
    """The recipe of `Book.objects.filter(*args, **kwargs)` (or `exclude()`)."""
    step = arguments.filter_step(Book, QueryRecipe(), "exclude" if negated else "filter", args, kwargs)
    assert step is not None
    return QueryRecipe().with_step(step)


def test_queries_that_differ_only_in_values_have_the_same_shape() -> None:
    assert filtered(pages__gt=500).shape == filtered(pages__gt=7).shape
    assert [value.value for value in filtered(pages__gt=500).values] == [500]
    sliced = filtered(pages__gt=500).with_step(arguments.slice_step(0, 20))
    assert sliced.shape != filtered(pages__gt=500).shape, "Django writes LIMIT into the SQL"


def test_the_number_of_in_values_is_part_of_the_shape() -> None:
    assert filtered(pages__in=[1, 2]).shape != filtered(pages__in=[1, 2, 3]).shape


def test_the_structure_of_the_filters_is_part_of_the_shape() -> None:
    either = filtered(Q(pages__gt=1) | Q(title="Dune"))
    both = filtered(Q(pages__gt=1) & Q(title="Dune"))
    neither = filtered(Q(pages__gt=1) | Q(title="Dune"), negated=True)
    assert len({either.shape, both.shape, neither.shape}) == 3
    assert either.shape == filtered(Q(pages__gt=500) | Q(title="Emma")).shape
    # A join through another relation is other SQL, though it ends at the same field.
    assert filtered(author__name="Jane").shape != filtered(author__id=1).shape


def test_in_without_values_matches_nothing_where_it_decides_the_answer() -> None:
    assert filtered(pages__in=[]).matches_nothing
    assert filtered(Q(pages__in=[]) & Q(title="Dune")).matches_nothing
    assert filtered(Q(pages__in=[]) | Q(title__in=[])).matches_nothing
    assert not filtered(Q(pages__in=[]) | Q(title="Dune")).matches_nothing
    assert not filtered(pages__in=[], negated=True).matches_nothing, "excluding nothing keeps everything"


def test_replay_builds_the_query_django_would_build() -> None:
    recorded = QuerySet(Book).filter(pages__gt=100, title="Dune").order_by("-pages").reverse()[2:5]
    assert recorded._recipe is not None, "every call was noted"
    django_query = Query(Book)
    django_query.add_q(Q(pages__gt=100, title="Dune"))
    django_query.clear_ordering(force=True, clear_default=False)
    django_query.add_ordering("-pages")
    django_query.standard_ordering = False
    django_query.set_limits(2, 5)
    assert str(recorded.query) == str(django_query)
    assert recorded._recipe is None, "asking for the query built it"


def make_queryset(model_queryset: Any) -> Any:
    """The same calls, on a djox or a Django `QuerySet`."""
    return (
        model_queryset.filter(Q(pages__gt=100) | ~Q(author__name="Jane"), in_print=True)
        .exclude(title__in=["Dune", "Emma"], author__isnull=True)
        .annotate(editions_count=Count("editions"), longer=F("pages") + 10)
        .filter(editions_count__gt=0, editions__number__lt=3)
        .alias(cheap=F("price") / 2)
        .order_by("author__name", F("longer").desc())
        .select_related("author")
        .values("title", "editions_count")
        .distinct()
    )


def test_replay_builds_djangos_query_for_any_calls() -> None:
    recorded = make_queryset(QuerySet(Book))
    assert recorded._recipe is not None, "every call was noted"
    assert str(recorded.query) == str(make_queryset(models.QuerySet(Book)).query)


def test_prefetch_related_keeps_the_recipe() -> None:
    # `Any`: django-stubs doesn't know Django's private `_prefetch_related_lookups`.
    recorded: Any = QuerySet(Book).filter(pages__gt=100).prefetch_related("editions").prefetch_related("author")
    assert recorded._recipe is not None, "prefetching doesn't change the query"
    assert recorded._prefetch_related_lookups == ("editions", "author")
    assert recorded.prefetch_related(None)._prefetch_related_lookups == ()
