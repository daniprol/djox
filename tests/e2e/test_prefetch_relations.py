"""`prefetch_related()` of a forward foreign key and of a many-to-many field.

Their second query takes the fast lane, like the one of a reverse foreign
key (`test_related_objects.py`): Django's descriptors write those queries in
ways that leave it (`djox._prefetch_forward`, `djox._prefetch_many_to_many`).
The models are the `shelf` app's, written as if djox didn't exist.

Each test checks that the objects are exactly Django's (computed with the
fast lane off, `djangos()`), and that using them runs no more queries.
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.db import models
from django.db.models import Prefetch

from djox._fast_lane import statements
from tests.e2e.shelf.models import Box, Label, Shelf
from tests.e2e.test_fast_lane import djangos
from tests.e2e.test_related_objects import fast_statements, recorded_queries

pytestmark = pytest.mark.django_db(transaction=True)

MakeQuerySet = Callable[[], Any]


@pytest.fixture(autouse=True)
def no_statements_yet() -> Iterator[None]:
    """Each test starts with the fast lane knowing no shape yet."""
    statements.statements.clear()
    yield
    statements.statements.clear()


@pytest.fixture
def shelves() -> None:
    """Three shelves (one without labels), three labels, and four boxes."""
    oak, pine, bare = (
        Shelf.objects.create(name=name, width=width) for name, width in [("oak", 120), ("pine", 90), ("bare", 80)]
    )
    fragile, heavy, old = (Label.objects.create(text=text) for text in ["fragile", "heavy", "old"])
    oak.labels.add(fragile, heavy)
    pine.labels.add(heavy)
    for shelf, contents in [(oak, "plates"), (oak, "books"), (pine, "tools"), (bare, "dust")]:
        Box.objects.create(shelf=shelf, contents=contents)
    old.shelves.add(pine)
    statements.statements.clear()  # the tests count the statements of their own queries


def sql_of(table: str) -> list[str]:
    """The SQL of the fast lane's statements that read `table`."""
    return [found.sql for found in fast_statements() if f'FROM "{table}"' in found.sql]


def shelf_names(boxes: list[Box]) -> list[tuple[str, str]]:
    """Each box's contents and its shelf's name (the shelf comes from the prefetch)."""
    return [(box.contents, box.shelf.name) for box in boxes]


def label_texts(shelves: list[Shelf]) -> list[tuple[str, list[str]]]:
    return [(shelf.name, sorted(label.text for label in shelf.labels.all())) for shelf in shelves]


def shelf_texts(labels: list[Label]) -> list[tuple[str, list[str]]]:
    return [(label.text, sorted(shelf.name for shelf in label.shelves.all())) for label in labels]


# ---------------------------------------------------------------- forward foreign keys


@pytest.mark.usefixtures("shelves")
async def test_prefetching_a_foreign_key_gives_djangos_objects() -> None:
    def make_queryset() -> Any:
        return Box.objects.order_by("pk").prefetch_related("shelf")

    expected = await sync_to_async(djangos)(lambda: shelf_names(list(make_queryset())))
    statements.statements.clear()
    boxes = [box async for box in make_queryset()]
    with recorded_queries() as queries:
        assert (
            shelf_names(boxes) == expected == [("plates", "oak"), ("books", "oak"), ("tools", "pine"), ("dust", "bare")]
        )
    assert queries == [], "using the shelves runs no more queries"
    assert len(fast_statements()) == 2, "the boxes' query, and the shelves' query"


@pytest.mark.usefixtures("shelves")
def test_the_query_of_a_foreign_key_is_a_filter_on_the_key_and_reuses_its_sql() -> None:
    list(Box.objects.order_by("pk").prefetch_related("shelf"))
    [shelves_sql] = sql_of("shelf_shelf")
    assert 'WHERE "shelf_shelf"."id" IN (%s, %s, %s)' in shelves_sql, "the three shelves the boxes are on"
    list(Box.objects.order_by("pk").prefetch_related("shelf"))
    assert len(fast_statements()) == 2, "the second time, both shapes were known already"


@pytest.mark.usefixtures("shelves")
def test_two_boxes_of_one_shelf_ask_for_that_shelf_once() -> None:
    boxes = list(Box.objects.filter(shelf__name="oak").prefetch_related("shelf"))
    assert shelf_names(boxes) == [("plates", "oak"), ("books", "oak")]
    [shelves_sql] = sql_of("shelf_shelf")
    assert shelves_sql.count("%s") == 1


@pytest.mark.usefixtures("shelves")
def test_prefetching_no_boxes_runs_no_second_query() -> None:
    with recorded_queries() as queries:
        assert list(Box.objects.filter(contents="nothing").prefetch_related("shelf")) == []
    assert len(queries) == 1


@pytest.mark.usefixtures("shelves")
def test_a_prefetch_object_with_its_own_filter_is_djangos_answer() -> None:
    def make_queryset() -> Any:
        wide = Shelf.objects.filter(width__gte=100)
        return Box.objects.order_by("pk").prefetch_related(Prefetch("shelf", queryset=wide, to_attr="wide_shelf"))

    def describe(boxes: list[Box]) -> list[Any]:
        return [(box.contents, [shelf.name for shelf in [box.wide_shelf] if shelf]) for box in boxes]  # type: ignore[attr-defined]

    expected = djangos(lambda: describe(list(make_queryset())))
    assert (
        describe(list(make_queryset()))
        == expected
        == [("plates", ["oak"]), ("books", ["oak"]), ("tools", []), ("dust", [])]
    )
    assert len(sql_of("shelf_shelf")) == 1, "the shelves' query ran on the fast lane"


@pytest.mark.usefixtures("shelves")
def test_a_sliced_prefetch_queryset_is_left_to_django_which_refuses_to_filter_it() -> None:
    def make_queryset() -> Any:
        return Box.objects.order_by("pk").prefetch_related(
            Prefetch("shelf", queryset=Shelf.objects.order_by("name")[:5])
        )

    with pytest.raises(TypeError, match="Cannot filter a query once a slice has been taken"):
        djangos(lambda: list(make_queryset()))
    with pytest.raises(TypeError, match="Cannot filter a query once a slice has been taken"):
        list(make_queryset())


@pytest.mark.usefixtures("shelves")
def test_a_prefetch_queryset_that_is_not_djoxs_is_left_to_django() -> None:
    plain_shelves: models.QuerySet[Shelf] = models.QuerySet(Shelf).order_by("name")
    boxes = list(Box.objects.order_by("pk").prefetch_related(Prefetch("shelf", queryset=plain_shelves)))
    assert shelf_names(boxes) == [("plates", "oak"), ("books", "oak"), ("tools", "pine"), ("dust", "bare")]
    assert sql_of("shelf_shelf") == []


# ---------------------------------------------------------------- many-to-many


@pytest.mark.usefixtures("shelves")
async def test_prefetching_a_many_to_many_gives_djangos_objects() -> None:
    def make_queryset() -> Any:
        return Shelf.objects.order_by("pk").prefetch_related("labels")

    expected = await sync_to_async(djangos)(lambda: label_texts(list(make_queryset())))
    statements.statements.clear()
    found = [shelf async for shelf in make_queryset()]
    with recorded_queries() as queries:
        assert label_texts(found) == expected
    assert expected == [("oak", ["fragile", "heavy"]), ("pine", ["heavy", "old"]), ("bare", [])]
    assert queries == [], "using the labels runs no more queries"
    assert len(fast_statements()) == 2, "the shelves' query, and the labels' query"


@pytest.mark.usefixtures("shelves")
def test_the_query_of_a_many_to_many_is_a_filter_on_the_relation_with_the_owner_column() -> None:
    list(Shelf.objects.order_by("pk").prefetch_related("labels"))
    [labels_sql] = sql_of("shelf_label")
    assert "shelf_shelf_labels" in labels_sql
    assert labels_sql.count("JOIN") == 1, "the filter and the owner column share one join"
    list(Shelf.objects.order_by("pk").prefetch_related("labels"))
    assert len(fast_statements()) == 2, "the second time, both shapes were known already"


@pytest.mark.usefixtures("shelves")
def test_prefetching_from_the_other_side_gives_djangos_objects() -> None:
    def make_queryset() -> Any:
        return Label.objects.order_by("pk").prefetch_related("shelves")

    expected = djangos(lambda: shelf_texts(list(make_queryset())))
    statements.statements.clear()
    assert (
        shelf_texts(list(make_queryset()))
        == expected
        == [
            ("fragile", ["oak"]),
            ("heavy", ["oak", "pine"]),
            ("old", ["pine"]),
        ]
    )
    assert len(sql_of("shelf_shelf")) == 1, "the shelves' query ran on the fast lane"


@pytest.mark.usefixtures("shelves")
def test_a_prefetch_object_with_a_plain_queryset_takes_the_fast_lane() -> None:
    labels = Label.objects.all()
    shelves = list(Shelf.objects.order_by("pk").prefetch_related(Prefetch("labels", queryset=labels)))
    assert label_texts(shelves)[0] == ("oak", ["fragile", "heavy"])
    assert len(sql_of("shelf_label")) == 1


@pytest.mark.usefixtures("shelves")
def test_a_prefetch_object_with_its_own_filter_on_a_many_to_many_is_djangos_answer() -> None:
    def make_queryset() -> Any:
        labels = Label.objects.filter(text__startswith="h").order_by("-text")
        return Shelf.objects.order_by("pk").prefetch_related(Prefetch("labels", queryset=labels))

    expected = djangos(lambda: label_texts(list(make_queryset())))
    assert label_texts(list(make_queryset())) == expected == [("oak", ["heavy"]), ("pine", ["heavy"]), ("bare", [])]
    assert sql_of("shelf_label") == [], "left to Django: a join made by the filter could be reused"


@pytest.mark.usefixtures("shelves")
def test_a_filter_through_the_same_relation_is_not_mixed_up_with_the_prefetch() -> None:
    """Django reuses the join a `Prefetch()` queryset made through the relation; a new `filter()` would not."""

    def make_queryset() -> Any:
        labels = Label.objects.filter(shelves__name="pine")
        return Shelf.objects.order_by("pk").prefetch_related(Prefetch("labels", queryset=labels))

    expected = djangos(lambda: label_texts(list(make_queryset())))
    assert label_texts(list(make_queryset())) == expected


@pytest.mark.usefixtures("shelves")
def test_prefetching_works_in_sync_code_too() -> None:
    shelves = list(Shelf.objects.order_by("pk").prefetch_related("labels"))
    assert label_texts(shelves)[0] == ("oak", ["fragile", "heavy"])
    boxes = list(Box.objects.order_by("pk").prefetch_related("shelf"))
    assert shelf_names(boxes)[0] == ("plates", "oak")
    assert len(fast_statements()) == 4
