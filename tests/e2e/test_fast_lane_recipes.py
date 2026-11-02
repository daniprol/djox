"""The general fast lane: any chain of calls (`annotate()`, `F()`, aggregates,
filters through relations to many rows) reuses Django's SQL per shape.

Each test checks that the answer is exactly Django's (computed with the fast
lane off, `djangos()`), and that the fast lane really ran it, or stepped aside.
"""

import datetime
import decimal
import zoneinfo
from collections.abc import Callable
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.core.exceptions import FieldError
from django.db.models import Avg, Count, F, Func, IntegerField, Max, Q, Sum, Value
from django.db.models.functions import Coalesce, Lower, TruncDay
from django.utils import timezone

from djox._fast_lane import checks, statements
from djox._fast_lane.statements import Statement
from tests.e2e.library.models import Author, Book, Edition
from tests.e2e.shelf.models import Label, Shelf
from tests.e2e.test_fast_lane import PUBLISHED, djangos, fast_statements, fields_of

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


@pytest.fixture
def shelves() -> None:
    """Shelves with labels: a many-to-many field."""
    new, old, big = Label.objects.create(text="new"), Label.objects.create(text="old"), Label.objects.create(text="big")
    kitchen = Shelf.objects.create(name="kitchen", width=120)
    kitchen.labels.add(new, big)
    hall = Shelf.objects.create(name="hall", width=60)
    hall.labels.add(old)
    Shelf.objects.create(name="attic", width=200)


def answer_of(item: Any) -> Any:
    """Something to compare: an instance's fields (annotations included), or the item itself."""
    if hasattr(item, "_state"):
        return fields_of(item)
    return item


# ---------------------------------------------------------------- reading rows

RECIPES: dict[str, MakeQuerySet] = {
    "annotate a count": lambda: Book.objects.annotate(edition_count=Count("editions")).order_by("pk"),
    "filter on an annotation": lambda: (
        Book.objects.annotate(edition_count=Count("editions")).filter(edition_count__gte=2).order_by("pk")
    ),
    "order by an annotation": lambda: Book.objects.annotate(edition_count=Count("editions")).order_by(
        "-edition_count", "title"
    )[:3],
    "an annotation with a value": lambda: Book.objects.annotate(longer=F("pages") + 10).filter(longer__gt=420),
    "an expression as a filter value": lambda: Book.objects.filter(pages__gt=F("author_id") * 300).order_by("pk"),
    "alias, then filter on it": lambda: Book.objects.alias(half=F("pages") / 2).filter(half__lt=200).order_by("pk"),
    "a reverse foreign key": lambda: Book.objects.filter(editions__number=2).order_by("pk"),
    "a reverse foreign key, excluded": lambda: Book.objects.exclude(editions__number=2).order_by("pk"),
    "two hops back": lambda: Author.objects.filter(books__editions__number=3).order_by("pk"),
    "distinct": lambda: Author.objects.filter(books__pages__gt=100).distinct().order_by("pk"),
    "values with an annotation": lambda: (
        Author.objects.values("name").annotate(total_pages=Sum("books__pages")).order_by("name")
    ),
    "values_list, annotation first": lambda: (
        Book.objects.annotate(edition_count=Count("editions")).values_list("edition_count", "title").order_by("pk")
    ),
    "values with an expression": lambda: Book.objects.values("title", lower_title=Lower("title")).order_by("pk"),
    "a conditional aggregate": lambda: Author.objects.annotate(
        long_books=Count("books", filter=Q(books__pages__gt=300))
    ).order_by("pk"),
    "order by an expression": lambda: Book.objects.order_by(F("pages").desc(nulls_last=True)),
}


@pytest.mark.parametrize("make_queryset", RECIPES.values(), ids=RECIPES.keys())
@pytest.mark.usefixtures("library")
async def test_recipes_give_djangos_rows(make_queryset: MakeQuerySet) -> None:
    expected = await sync_to_async(djangos)(lambda: [answer_of(item) for item in make_queryset()])
    queryset = make_queryset()
    assert queryset._recipe is not None, "every call was noted for the fast lane"
    found = [answer_of(item) async for item in queryset]
    assert queryset._recipe is not None, "the fast lane ran it: Django's Query was never built"
    assert found == expected
    assert len(fast_statements()) == 1


@pytest.mark.parametrize("make_queryset", RECIPES.values(), ids=RECIPES.keys())
@pytest.mark.usefixtures("library")
def test_sync_recipes_give_djangos_rows_counts_and_first(make_queryset: MakeQuerySet) -> None:
    def answers() -> dict[str, Any]:
        return {
            "rows": [answer_of(item) for item in make_queryset()],
            "count": make_queryset().count(),
            "exists": make_queryset().exists(),
            "first": answer_of(make_queryset().first()),
        }

    assert djangos(answers) == answers()
    kinds = {key[2] for key, found in statements.statements.items() if isinstance(found, Statement)}
    if make_queryset is RECIPES["a conditional aggregate"]:
        # `count()` and `exists()` leave out an annotation nothing uses, and
        # so the `300` of its filter. A value missing from the SQL: the fast
        # lane can't tell where it went, and leaves those two to Django.
        assert kinds == {"rows"}
    else:
        assert kinds == {"rows", "count", "exists"}


@pytest.mark.usefixtures("shelves")
def test_many_to_many_filters_give_djangos_rows() -> None:
    def answers() -> dict[str, Any]:
        return {
            "labelled": list(Shelf.objects.filter(labels__text="big").values_list("name", flat=True)),
            "not labelled": list(Shelf.objects.exclude(labels__text="old").order_by("name").values_list("name")),
            "by label": list(Shelf.objects.filter(labels=Label.objects.get(text="new")).values("name")),
            "label counts": list(Shelf.objects.annotate(label_count=Count("labels")).order_by("name").values()),
            "back": list(Label.objects.filter(shelves__width__gt=100).order_by("text").values_list("text", flat=True)),
        }

    assert djangos(answers) == answers()
    assert len(fast_statements()) == 6, "five filters, and the get() of a label"


@pytest.mark.usefixtures("library")
async def test_queries_differing_only_in_values_share_their_statement() -> None:
    def with_more_than(editions: int) -> Any:
        return Book.objects.annotate(edition_count=Count("editions")).filter(edition_count__gt=editions)

    assert [book.title async for book in with_more_than(1).order_by("pk")] == ["Dune", "Emma"]
    assert [book.title async for book in with_more_than(2).order_by("pk")] == ["Dune"]
    assert len(fast_statements()) == 1


# ---------------------------------------------------------------- aggregate()

AGGREGATES: dict[str, Callable[[], tuple[Any, dict[str, Any]]]] = {
    "sum": lambda: (Book.objects.filter(pages__gt=300), {"total": Sum("pages")}),
    "several": lambda: (Book.objects.all(), {"most": Max("pages"), "average": Avg("price"), "count": Count("pk")}),
    "over an annotation": lambda: (
        Book.objects.annotate(edition_count=Count("editions")),
        {"most": Max("edition_count")},
    ),
    "with a filter inside": lambda: (Book.objects.all(), {"long": Count("pk", filter=Q(pages__gt=400))}),
    "with a default": lambda: (Book.objects.filter(pages__gt=10_000), {"total": Sum("pages", default=0)}),
    "nothing matches": lambda: (Book.objects.filter(pages__gt=10_000), {"total": Sum("pages")}),
}


@pytest.mark.parametrize("make_aggregate", AGGREGATES.values(), ids=AGGREGATES.keys())
@pytest.mark.usefixtures("library")
async def test_aggregates_are_djangos(make_aggregate: Callable[[], tuple[Any, dict[str, Any]]]) -> None:
    def sync_answer() -> dict[str, Any]:
        queryset, aggregates = make_aggregate()
        result: dict[str, Any] = queryset.aggregate(**aggregates)
        return result

    expected = await sync_to_async(djangos)(sync_answer)
    queryset, aggregates = make_aggregate()
    assert await queryset.aaggregate(**aggregates) == expected
    assert await sync_to_async(sync_answer)() == expected
    assert len(fast_statements()) == 1, "both ran the statement the fast lane kept"


@pytest.mark.usefixtures("library")
def test_an_aggregate_without_an_alias_is_named_like_djangos() -> None:
    assert Book.objects.aggregate(Sum("pages")) == {"pages__sum": 412 + 256 + 474 + 249}
    with pytest.raises(TypeError, match="Complex aggregates require an alias"):
        Book.objects.aggregate(Sum(F("pages") + 1))


@pytest.mark.usefixtures("library")
def test_an_aggregate_of_nothing_is_djangos() -> None:
    assert Book.objects.filter(pages__in=[]).aggregate(total=Sum("pages"), count=Count("pk")) == {
        "total": None,
        "count": 0,
    }


# ---------------------------------------------------------------- update()


@pytest.mark.usefixtures("library")
async def test_update_with_f_runs_on_the_fast_lane() -> None:
    assert await Book.objects.filter(title="Dune").aupdate(pages=F("pages") + 10) == 1
    assert await Book.objects.filter(title="Emma").aupdate(pages=F("pages") + 26) == 1
    assert await Book.objects.filter(title="Dune").aupdate(pages=F("pages") * 2, price=decimal.Decimal("5.00")) == 1
    pages = {book.title: (book.pages, book.price) async for book in Book.objects.filter(title__in=["Dune", "Emma"])}
    assert pages == {"Dune": (844, decimal.Decimal("5.00")), "Emma": (500, decimal.Decimal("9.99"))}
    updates = [found for found in fast_statements() if found.sql.startswith("UPDATE")]
    assert len(updates) == 2, "one statement per shape of update"


@pytest.mark.usefixtures("library")
def test_an_update_whose_values_are_alike_is_learned_from_a_later_one() -> None:
    # The filter's `412` and the expression's `412`: which parameter is which?
    dune = Book.objects.get(title="Dune")
    assert Book.objects.filter(pages=412).update(pages=F("pages") + 412) == 1
    assert not [found for found in fast_statements() if found.sql.startswith("UPDATE")], "a doubt: Django's way"
    assert Book.objects.filter(pages=824).update(pages=F("pages") + 1) == 1
    assert [found for found in fast_statements() if found.sql.startswith("UPDATE")], "learned from the second"
    dune.refresh_from_db()
    assert dune.pages == 825


# ---------------------------------------------------------------- what stays Django's


@pytest.mark.usefixtures("library")
def test_expressions_of_the_programs_own_go_to_django() -> None:
    class Twice(Func):
        template = "(%(expressions)s * 2)"
        output_field = IntegerField()

    queryset = Book.objects.annotate(twice=Twice("pages")).order_by("pk")
    assert queryset._recipe is None  # type: ignore[attr-defined]  # djox.QuerySet's
    assert [book.twice for book in queryset] == [824, 512, 948, 498]


@pytest.mark.usefixtures("library")
def test_a_value_written_into_the_sql_is_left_to_django() -> None:
    # `alias()` that isn't used: Django leaves its value out of the SQL.
    queryset = Book.objects.alias(one=Value(1)).order_by("pk")
    assert [book.title for book in queryset] == ["Dune", "Dune Messiah", "Emma", "Persuasion"]
    assert [book.title for book in queryset.all()] == ["Dune", "Dune Messiah", "Emma", "Persuasion"]
    assert fast_statements() == []


# ---------------------------------------------------------------- Django's checks


def test_django_checks_each_new_kind_of_call_when_it_is_made() -> None:
    # Twice: a call Django refused is checked again.
    for _ in range(2):
        with pytest.raises(FieldError, match="Cannot resolve keyword 'nothing'"):
            Book.objects.annotate(n=Count("nothing"))
    with pytest.raises(ValueError, match="conflicts with a field"):
        Book.objects.annotate(pages=Count("editions"))  # type: ignore[no-redef]  # on purpose
    with pytest.raises(FieldError, match="Cannot resolve keyword 'nothing'"):
        Book.objects.order_by("nothing")  # type: ignore[misc]  # on purpose
    with pytest.raises(TypeError, match="Cannot reorder a query once a slice has been taken"):
        Book.objects.all()[:2].order_by("pages")
    with pytest.raises(ValueError, match="expected a number but got 'many'"):
        Book.objects.annotate(n=Count("editions")).filter(n__gt="many")  # type: ignore[misc]  # on purpose


def test_a_checked_call_is_remembered_with_any_values() -> None:
    checks.checked.clear()
    Book.objects.annotate(longer=F("pages") + 1)
    Book.objects.annotate(longer=F("pages") + 2)
    assert len(checks.checked) == 1


# ---------------------------------------------------------------- the time zone


@pytest.mark.usefixtures("library")
def test_the_time_zone_is_part_of_the_key_of_a_query_with_expressions() -> None:
    late = datetime.datetime(2020, 1, 1, 23, 30, tzinfo=datetime.UTC)
    Book.objects.filter(title="Dune").update(published=late)

    def days() -> list[Any]:
        return list(Book.objects.filter(title="Dune").annotate(day=TruncDay("published")).values_list("day", flat=True))

    zones = ["UTC", "Asia/Tokyo"]
    expected = {}
    for zone in zones:
        with timezone.override(zoneinfo.ZoneInfo(zone)):
            expected[zone] = djangos(days)
    assert expected["UTC"] != expected["Asia/Tokyo"], "the day depends on the time zone"
    for zone in zones:
        with timezone.override(zoneinfo.ZoneInfo(zone)):
            assert days() == expected[zone]
    assert len(fast_statements()) == 2, "one statement per time zone"


@pytest.mark.usefixtures("library")
def test_coalesce_and_case_like_expressions_are_djangos() -> None:
    def answers() -> list[Any]:
        return list(
            Book.objects.annotate(best=Coalesce(Max("editions__number"), Value(0)))
            .order_by("pk")
            .values_list("title", "best")
        )

    assert djangos(answers) == answers()
    assert len(fast_statements()) == 1
