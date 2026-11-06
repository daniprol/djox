"""The fast lane's filters: `Q` objects, `exclude()`, and names through foreign keys.

Each test checks two things, like `test_fast_lane.py`: the answer is
exactly Django's (computed with the fast lane off, `djangos()`), and the
fast lane really ran (or really stepped aside).
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.core.exceptions import FieldError
from django.db import connection, transaction
from django.db.models import F, Q
from django.db.models.lookups import GreaterThan
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from djox._fast_lane import statements
from tests.e2e.library.models import Author, Book, Edition, Publisher
from tests.e2e.test_fast_lane import PUBLISHED, djangos, fast_statements, fields_of

# A function that makes a fresh queryset. `Any`: django-stubs types the
# querysets as Django's, without djox's attributes.
MakeQuerySet = Callable[[], Any]

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def no_statements_yet() -> Iterator[None]:
    """Each test starts with the fast lane knowing no shape yet."""
    statements.statements.clear()
    yield
    statements.statements.clear()


@pytest.fixture
def library() -> None:
    """Two authors, three books, and four editions (two without a publisher)."""
    herbert = Author.objects.create(name="Frank Herbert")
    austen = Author.objects.create(name="Jane Austen")
    ace = Publisher.objects.create(name="Ace")
    for title, pages, author, in_print in [
        ("Dune", 412, herbert, True),
        ("Dune Messiah", 256, herbert, False),
        ("Emma", 474, austen, True),
    ]:
        Book.objects.create(
            title=title, pages=pages, price="9.99", published=PUBLISHED, author=author, in_print=in_print
        )
    dune = Book.objects.get(title="Dune")
    Edition.objects.create(book=dune, publisher=ace, number=1)
    Edition.objects.create(book=dune, publisher=None, number=2)
    Edition.objects.create(book=Book.objects.get(title="Emma"), publisher=None, number=1)
    Edition.objects.create(book=Book.objects.get(title="Dune Messiah"), publisher=ace, number=1)
    statements.statements.clear()  # the tests count the statements of their own queries


# Querysets the fast lane runs itself. Each is a function, so each test gets a fresh one.
FILTERED_QUERYSETS: dict[str, MakeQuerySet] = {
    "Q with or": lambda: Book.objects.filter(Q(pages__gt=450) | Q(title="Dune")),
    "negated Q": lambda: Book.objects.filter(~Q(in_print=True)),
    "Q and keywords": lambda: Book.objects.filter(Q(title__in=["Dune", "Emma"]), pages__gt=300),
    "nested Q": lambda: Book.objects.filter(Q(pages__gt=300) & (Q(title="Emma") | ~Q(author__name="Jane Austen"))),
    "exclude": lambda: Book.objects.exclude(in_print=True),
    "exclude several": lambda: Book.objects.exclude(pages__lt=300, in_print=False),
    "exclude after filter": lambda: Book.objects.filter(pages__gt=300).exclude(title="Dune"),
    "a join": lambda: Book.objects.filter(author__name="Jane Austen"),
    "a join and a lookup": lambda: Book.objects.filter(author__name__in=["Jane Austen", "Nobody"]),
    "exclude through a join": lambda: Book.objects.exclude(author__name="Jane Austen"),
    "two joins": lambda: Edition.objects.filter(book__author__name="Frank Herbert"),
    "a nullable join, isnull": lambda: Edition.objects.filter(publisher__name__isnull=True),
    "exclude through a nullable join": lambda: Edition.objects.exclude(publisher__name="Ace"),
    "or across joins": lambda: Edition.objects.filter(Q(publisher__name="Ace") | Q(book__title="Emma")),
    "ordered through joins": lambda: Edition.objects.order_by("-book__author__name", "book__title", "number"),
    "an empty in inside or": lambda: Book.objects.filter(Q(pk__in=[]) | Q(title="Emma")),
    "exclude an empty in": lambda: Book.objects.exclude(pk__in=[]),
}


@pytest.mark.parametrize("make_queryset", FILTERED_QUERYSETS.values(), ids=FILTERED_QUERYSETS.keys())
@pytest.mark.usefixtures("library")
async def test_filters_give_djangos_objects(make_queryset: MakeQuerySet) -> None:
    expected = await sync_to_async(djangos)(lambda: [fields_of(item) for item in make_queryset()])
    queryset = make_queryset()
    assert queryset._recipe is not None, "every step was noted for the fast lane"
    found = [fields_of(item) async for item in queryset]
    assert queryset._recipe is not None, "the fast lane ran it: Django's Query was never built"
    assert found == expected
    assert len(fast_statements()) == 1


@pytest.mark.parametrize("make_queryset", FILTERED_QUERYSETS.values(), ids=FILTERED_QUERYSETS.keys())
@pytest.mark.usefixtures("library")
def test_sync_filters_give_djangos_objects(make_queryset: MakeQuerySet) -> None:
    expected = djangos(lambda: [fields_of(item) for item in make_queryset()])
    assert [fields_of(item) for item in make_queryset()] == expected
    assert len(fast_statements()) == 1


@pytest.mark.parametrize("make_queryset", FILTERED_QUERYSETS.values(), ids=FILTERED_QUERYSETS.keys())
@pytest.mark.usefixtures("library")
async def test_filtered_counts_and_exists_are_djangos(make_queryset: MakeQuerySet) -> None:
    expected_count = await sync_to_async(djangos)(lambda: make_queryset().count())
    assert await make_queryset().acount() == expected_count
    assert await make_queryset().aexists() is (expected_count > 0)
    assert len(fast_statements()) == 2, "one count and one exists statement"


@pytest.mark.usefixtures("library")
async def test_or_and_and_are_different_shapes_and_values_are_not() -> None:
    assert await Book.objects.filter(Q(pages__gt=300) | Q(title="Dune")).acount() == 2
    assert await Book.objects.filter(Q(pages__gt=100) | Q(title="Emma")).acount() == 3
    assert await Book.objects.filter(Q(pages__gt=300) & Q(title="Dune")).acount() == 1
    assert len(fast_statements()) == 2, "or, and and: two shapes"


@pytest.mark.usefixtures("library")
async def test_aget_takes_q_objects() -> None:
    expected = await sync_to_async(djangos)(lambda: Book.objects.get(Q(title="Dune") | Q(title="Ulysses")))
    dune = await Book.objects.aget(Q(title="Dune") | Q(title="Ulysses"))
    assert fields_of(dune) == fields_of(expected)
    with pytest.raises(Book.MultipleObjectsReturned):
        await Book.objects.aget(~Q(title="Dune"))
    assert len(fast_statements()) == 2


@pytest.mark.usefixtures("library")
def test_excluding_none_is_another_shape_than_excluding_nothing() -> None:
    # Django 6.1 writes `OR "publisher_id" IS NULL` when the values include `None`.
    def counts() -> list[int]:
        return [Edition.objects.exclude(publisher__in=values).count() for values in ([], [None], [None, -1])]

    assert counts() == djangos(counts)


@pytest.mark.usefixtures("library")
def test_nothing_runs_when_an_empty_in_decides_the_answer() -> None:
    with CaptureQueriesContext(connection) as captured:
        assert list(Book.objects.filter(Q(pk__in=[]) & Q(pages__gt=1))) == []
        assert Book.objects.filter(Q(pk__in=[]) | Q(title__in=[])).count() == 0
    assert captured.captured_queries == []


def update_and_delete_through_joins() -> tuple[int, int, list[int]]:
    """What a filtered `update()` and `delete()` return, and the pages of every book after them."""
    updated = Book.objects.filter(author__name="Jane Austen").update(pages=1)
    deleted, _ = Edition.objects.filter(Q(publisher__name="Ace") | Q(number=2)).delete()
    return updated, deleted, list(Book.objects.order_by("pk").values_list("pages", flat=True))


@pytest.mark.usefixtures("library")
def test_writes_filter_through_joins_like_django() -> None:
    with override_settings(DJOX_FAST_LANE=False), transaction.atomic():
        expected = update_and_delete_through_joins()
        transaction.set_rollback(True)
    assert update_and_delete_through_joins() == expected
    assert any("UPDATE" in found.sql for found in fast_statements()), "the fast lane ran the update"
    assert any("DELETE" in found.sql for found in fast_statements()), "and the delete"


@pytest.mark.usefixtures("library")
async def test_changing_a_list_inside_q_after_filter_changes_nothing() -> None:
    titles = ["Emma"]
    queryset = Book.objects.filter(Q(title__in=titles) | Q(pages=1))
    titles.append("Dune")
    assert await queryset.acount() == 1
    # Django's `Query`, built from the note, has the list as it was too.
    assert "Emma" in str(queryset.query)
    assert "Dune" not in str(queryset.query)


def test_errors_are_raised_where_django_raises_them() -> None:
    with pytest.raises(ValueError, match="expected a number"):
        Book.objects.filter(Q(title="Dune") | Q(pages="many"))
    with pytest.raises(ValueError, match="expected a number"):
        Book.objects.exclude(pages="many")
    with pytest.raises(FieldError, match="Unsupported lookup 'nothing'"):
        Book.objects.exclude(author__nothing=1)
    with pytest.raises(TypeError, match="Cannot filter a query once a slice has been taken"):
        Book.objects.all()[:2].exclude(pages=1)


# Querysets the fast lane leaves to Django, which still gives the right answer.
DJANGO_QUERYSETS: dict[str, MakeQuerySet] = {
    "a transform": lambda: Book.objects.filter(published__year=1965),
    "a transform through a join": lambda: Book.objects.filter(author__name__icontains="jane"),
    "a lookup as an expression in Q": lambda: Book.objects.filter(Q(GreaterThan(F("pages"), 300)) | Q(title="Emma")),
    "an empty Q in Q": lambda: Book.objects.filter(Q(Q()) | Q(title="Emma")),
}


XOR = pytest.param(
    lambda: Book.objects.filter(Q(pages__gt=300) ^ Q(title="Dune")),
    id="XOR",
    # Postgres has no XOR: Django writes `CASE WHEN ... THEN %s` sums, and
    # djox's async driver sends those numbers as text (a known gap, not the fast lane's).
    marks=pytest.mark.sqlite_only("djox's Postgres driver sends the numbers of Django's XOR as text"),
)


@pytest.mark.parametrize(
    "make_queryset",
    [*(pytest.param(make, id=name) for name, make in DJANGO_QUERYSETS.items()), XOR],
)
@pytest.mark.usefixtures("library")
async def test_other_filters_go_to_django(make_queryset: MakeQuerySet) -> None:
    expected = await sync_to_async(djangos)(lambda: list(make_queryset().order_by("pk")))
    books = [book async for book in make_queryset().order_by("pk")]
    assert [book.pk for book in books] == [book.pk for book in expected]
    assert fast_statements() == []
