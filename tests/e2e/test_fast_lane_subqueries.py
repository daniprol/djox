"""Subqueries on the fast lane: `Exists()`, `Subquery()`, `OuterRef()` and `pk__in=queryset`.

Each test checks, like the other fast lane tests, that the answer is exactly
Django's (computed with the fast lane off, `djangos()`), and that the fast
lane really ran it, or stepped aside.
"""

import decimal
from collections.abc import Callable
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.core.exceptions import FieldError
from django.db import models
from django.db.models import Count, Exists, OuterRef, Q, Subquery

from djox._fast_lane import statements
from djox._fast_lane.statements import Statement
from tests.e2e.library.models import Author, Book, Edition
from tests.e2e.test_fast_lane import PUBLISHED, djangos, fast_statements

# A function that makes a fresh queryset. `Any`: django-stubs types `Book.objects`'
# querysets as Django's, without djox's attributes.
MakeQuerySet = Callable[[], Any]

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def no_statements_yet() -> None:
    """Each test starts with the fast lane knowing no shape yet."""
    statements.statements.clear()
    statements.attempts.clear()


@pytest.fixture
def library() -> None:
    """Two authors, four books, and some editions."""
    herbert = Author.objects.create(name="Frank Herbert")
    austen = Author.objects.create(name="Jane Austen")
    for title, pages, author, editions in [
        ("Dune", 412, herbert, 3),
        ("Dune Messiah", 256, herbert, 1),
        ("Emma", 474, austen, 2),
        ("Persuasion", 249, austen, 0),
    ]:
        book = Book.objects.create(
            title=title, pages=pages, price=decimal.Decimal("9.99"), published=PUBLISHED, author=author
        )
        for number in range(1, editions + 1):
            Edition.objects.create(book=book, number=number)


def editions_from(number: int) -> Any:
    """The editions of the outer query's book numbered `number` or more."""
    return Edition.objects.filter(book=OuterRef("pk"), number__gte=number)


SUBQUERIES: dict[str, MakeQuerySet] = {
    "exists": lambda: Book.objects.filter(Exists(editions_from(2))).order_by("pk"),
    "not exists": lambda: Book.objects.filter(~Exists(editions_from(2))).order_by("pk"),
    "exists or a filter": lambda: Book.objects.filter(Q(Exists(editions_from(3))) | Q(pages__lt=300)).order_by("pk"),
    "exists, excluded": lambda: Book.objects.exclude(Exists(editions_from(2)), pages__gt=400).order_by("pk"),
    "exists as an annotation": lambda: Book.objects.annotate(reprinted=Exists(editions_from(2))).order_by("pk"),
    "the latest edition": lambda: Book.objects.annotate(
        latest=Subquery(Edition.objects.filter(book=OuterRef("pk")).order_by("-number").values("number")[:1])
    ).order_by("pk"),
    "a count in a subquery": lambda: Book.objects.annotate(
        edition_count=Subquery(
            Edition.objects.filter(book=OuterRef("pk")).values("book").annotate(count=Count("pk")).values("count")
        )
    ).order_by("pk"),
    "in a queryset": lambda: Author.objects.filter(pk__in=Book.objects.filter(pages__gt=450).values("author")),
    "not in a queryset": lambda: Book.objects.exclude(pk__in=Edition.objects.values("book")).order_by("pk"),
    "a subquery in a subquery": lambda: Author.objects.filter(
        Exists(Book.objects.filter(author=OuterRef("pk")).filter(Exists(editions_from(3))))
    ),
    "an outer reference two levels up": lambda: Author.objects.filter(
        Exists(Book.objects.filter(Exists(Edition.objects.filter(book__author=OuterRef(OuterRef("pk")), number=2))))
    ).order_by("pk"),
}


def answer_of(item: Any) -> Any:
    """Something to compare: an instance's fields (annotations included)."""
    return {name: value for name, value in item.__dict__.items() if name != "_state"}


@pytest.mark.parametrize("make_queryset", SUBQUERIES.values(), ids=SUBQUERIES.keys())
@pytest.mark.usefixtures("library")
async def test_subqueries_give_djangos_rows(make_queryset: MakeQuerySet) -> None:
    expected = await sync_to_async(djangos)(lambda: [answer_of(item) for item in make_queryset()])
    queryset = make_queryset()
    assert queryset._recipe is not None, "every call was noted for the fast lane"
    found = [answer_of(item) async for item in queryset]
    assert queryset._recipe is not None, "the fast lane ran it: Django's Query was never built"
    assert found == expected
    assert len(fast_statements()) == 1


@pytest.mark.parametrize("make_queryset", SUBQUERIES.values(), ids=SUBQUERIES.keys())
@pytest.mark.usefixtures("library")
def test_sync_subqueries_give_djangos_rows_and_counts(make_queryset: MakeQuerySet) -> None:
    def answers() -> dict[str, Any]:
        return {
            "rows": [answer_of(item) for item in make_queryset()],
            "count": make_queryset().count(),
            "exists": make_queryset().exists(),
        }

    assert djangos(answers) == answers()
    # (`count()` and `exists()` may leave out an annotation nothing uses, and
    # so its values: then the fast lane leaves those two to Django.)
    kinds = {key[2] for key, found in statements.statements.items() if isinstance(found, Statement)}
    assert "rows" in kinds


@pytest.mark.usefixtures("library")
async def test_subqueries_differing_only_in_values_share_their_statement() -> None:
    def reprinted(number: int) -> Any:
        return Book.objects.filter(Exists(editions_from(number))).order_by("pk")

    assert [book.title async for book in reprinted(2)] == ["Dune", "Emma"]
    assert [book.title async for book in reprinted(3)] == ["Dune"]
    assert [book.title async for book in reprinted(4)] == []
    assert len(fast_statements()) == 1


@pytest.mark.usefixtures("library")
async def test_writes_with_subqueries_run_on_the_fast_lane() -> None:
    never_reprinted = Book.objects.filter(~Exists(editions_from(2)))
    assert await never_reprinted.aupdate(in_print=False) == 2
    editions = Edition.objects.filter(book=OuterRef("pk"))
    latest = Subquery(editions.order_by("-number").values("number")[:1])
    assert await Book.objects.filter(Exists(editions), in_print=False).aupdate(pages=latest) == 1
    pages = {book.title: (book.pages, book.in_print) async for book in Book.objects.all()}
    assert pages == {
        "Dune": (412, True),
        "Dune Messiah": (1, False),
        "Emma": (474, True),
        "Persuasion": (249, False),
    }
    assert len(fast_statements()) == 3, "two updates and the read"


@pytest.mark.usefixtures("library")
def test_a_wrong_outer_name_raises_djangos_error_when_filter_runs() -> None:
    for _ in range(2):  # a call Django refused is checked again
        with pytest.raises(FieldError, match="Cannot resolve keyword 'nothing'"):
            Book.objects.filter(Exists(Edition.objects.filter(book=OuterRef("nothing"))))


# Subqueries the fast lane leaves to Django, which still gives the right answer.
DJANGO_SUBQUERIES: dict[str, MakeQuerySet] = {
    "of a plain Django queryset": lambda: Book.objects.filter(
        Exists(models.QuerySet(Edition).filter(book=OuterRef("pk"), number=2))
    ),
    "of a queryset Django changed": lambda: Book.objects.filter(
        Exists(editions_from(2).filter(book__title__icontains="dune"))
    ),
    "an outer name through a relation": lambda: Edition.objects.filter(
        Exists(Book.objects.filter(pk=OuterRef("book__pk"), pages__gt=400))
    ),
}


@pytest.mark.parametrize("make_queryset", DJANGO_SUBQUERIES.values(), ids=DJANGO_SUBQUERIES.keys())
@pytest.mark.usefixtures("library")
async def test_other_subqueries_go_to_django(make_queryset: MakeQuerySet) -> None:
    expected = await sync_to_async(djangos)(lambda: list(make_queryset().order_by("pk")))
    found = [item async for item in make_queryset().order_by("pk")]
    assert [item.pk for item in found] == [item.pk for item in expected]
    assert fast_statements() == []
