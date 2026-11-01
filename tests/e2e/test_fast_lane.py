"""The fast lane: simple queries reuse Django's SQL per shape, and Rust builds the objects.

Each test checks two things: the answer is exactly Django's (compared with
Django's sync methods, which build the query the usual way), and the fast
lane really ran (or really stepped aside).
"""

import asyncio
import datetime
import decimal
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import connection, models, transaction
from django.db.models import F, Q
from django.db.models.signals import post_init
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from djox._fast_lane import statements
from djox._fast_lane.statements import Statement
from djox._running import sql_with_parameters
from tests.e2e.library.models import Author, Book

# A function that makes a fresh queryset. `Any`: django-stubs types `Book.objects`'
# querysets as Django's, without djox's attributes.
MakeQuerySet = Callable[[], Any]

PUBLISHED = datetime.datetime(1965, 8, 1, 12, 0, tzinfo=datetime.UTC)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def no_statements_yet() -> Iterator[None]:
    """Each test starts with the fast lane knowing no shape yet."""
    statements.statements.clear()
    yield
    statements.statements.clear()


@pytest.fixture
def library() -> None:
    """Two authors and four books, written with Django's sync ORM."""
    herbert = Author.objects.create(name="Frank Herbert")
    austen = Author.objects.create(name="Jane Austen")
    for title, pages, author, in_print in [
        ("Dune", 412, herbert, True),
        ("Dune Messiah", 256, herbert, True),
        ("Emma", 474, austen, False),
        ("Persuasion", 249, austen, True),
    ]:
        Book.objects.create(
            title=title,
            pages=pages,
            price=decimal.Decimal("9.99"),
            published=PUBLISHED,
            in_print=in_print,
            author=author,
        )


def fields_of(book: models.Model) -> dict[str, Any]:
    """An instance's field values and state, to compare two instances."""
    values = {name: value for name, value in book.__dict__.items() if name != "_state"}
    return {**values, "db": book._state.db, "adding": book._state.adding}


def fast_statements() -> list[Statement]:
    """The statements the fast lane kept (shapes it runs itself)."""
    return [found for found in statements.statements.values() if isinstance(found, Statement)]


# Querysets the fast lane runs itself. Each is a function, so each test gets a fresh one.
SIMPLE_QUERYSETS: dict[str, MakeQuerySet] = {
    "all": lambda: Book.objects.all(),
    "gt and order_by": lambda: Book.objects.filter(pages__gt=300).order_by("-pages"),
    "two filters": lambda: Book.objects.filter(pages__lt=450).filter(in_print=True).order_by("title"),
    "in": lambda: Book.objects.filter(title__in=["Emma", "Dune", "Ulysses"]).order_by("pk"),
    "isnull": lambda: Book.objects.filter(title__isnull=False).order_by("id"),
    "exact None": lambda: Book.objects.filter(title=None),
    "boolean": lambda: Book.objects.filter(in_print=False),
    "decimal and datetime": lambda: Book.objects.filter(price=decimal.Decimal("9.99"), published__lte=PUBLISHED),
    "a slice": lambda: Book.objects.order_by("title")[1:3],
    "an open slice": lambda: Book.objects.order_by("title")[2:],
    "reverse": lambda: Book.objects.order_by("pages").reverse(),
    "foreign key by name": lambda: Book.objects.filter(author_id__in=[1, 2]).order_by("-id"),
    "ordered by a foreign key": lambda: Book.objects.order_by("author", "title"),
}


@pytest.mark.parametrize("make_queryset", SIMPLE_QUERYSETS.values(), ids=SIMPLE_QUERYSETS.keys())
@pytest.mark.usefixtures("library")
async def test_simple_querysets_give_djangos_objects(make_queryset: MakeQuerySet) -> None:
    queryset = make_queryset()
    assert queryset._simple_query is not None, "every step was noted for the fast lane"
    books = [book async for book in queryset]
    assert queryset._simple_query is not None, "the fast lane ran it: Django's Query was never built"
    expected = await sync_to_async(lambda: list(make_queryset()))()
    assert [fields_of(book) for book in books] == [fields_of(book) for book in expected]
    assert len(fast_statements()) == 1


@pytest.mark.parametrize("make_queryset", SIMPLE_QUERYSETS.values(), ids=SIMPLE_QUERYSETS.keys())
@pytest.mark.usefixtures("library")
async def test_counts_and_exists_are_djangos(make_queryset: MakeQuerySet) -> None:
    expected_count = await sync_to_async(lambda: make_queryset().count())()
    assert await make_queryset().acount() == expected_count
    assert await make_queryset().aexists() is (expected_count > 0)
    assert len(fast_statements()) == 2, "one count and one exists statement"


@pytest.mark.usefixtures("library")
async def test_aget_afirst_and_alast_take_the_fast_lane() -> None:
    dune = await Book.objects.aget(title="Dune")
    assert fields_of(dune) == fields_of(await sync_to_async(Book.objects.get)(title="Dune"))
    assert (await Book.objects.filter(pages__gt=300).order_by("pages").afirst()).title == "Dune"  # type: ignore[union-attr]
    assert (await Book.objects.order_by("pages").alast()).title == "Emma"  # type: ignore[union-attr]
    assert (await Book.objects.afirst()).title == "Dune", "no ordering: by primary key, like Django"  # type: ignore[union-attr]
    assert (await Book.objects.alast()).title == "Persuasion"  # type: ignore[union-attr]
    assert await Book.objects.filter(pages__gt=1000).afirst() is None
    assert len(fast_statements()) == 6


@pytest.mark.usefixtures("library")
async def test_aget_keeps_djangos_exceptions() -> None:
    with pytest.raises(Book.DoesNotExist, match=r"Book matching query does not exist\."):
        await Book.objects.aget(title="Ulysses")
    herbert = await Author.objects.aget(name="Frank Herbert")
    with pytest.raises(Book.MultipleObjectsReturned, match=r"it returned 2!"):
        await Book.objects.aget(author_id=herbert.id)


@pytest.mark.usefixtures("library")
async def test_a_shape_is_written_once_and_reused_with_new_values() -> None:
    for pages in [100, 250, 300, 450]:
        expected = await sync_to_async(lambda pages=pages: Book.objects.filter(pages__gt=pages).count())()
        assert await Book.objects.filter(pages__gt=pages).acount() == expected
    assert len(fast_statements()) == 1, "four queries, one shape"


@pytest.mark.usefixtures("library")
async def test_a_foreign_key_filter_takes_a_model_instance() -> None:
    herbert = await Author.objects.aget(name="Frank Herbert")
    titles = [book.title async for book in Book.objects.filter(author=herbert).order_by("title")]
    assert titles == ["Dune", "Dune Messiah"]


@pytest.mark.usefixtures("library")
def test_the_fast_lane_runs_djangos_exact_sql() -> None:
    def make_queryset() -> Any:
        return Book.objects.filter(pages__gt=300, in_print=True).order_by("-pages")[:2]

    sql, parameters = make_queryset().query.get_compiler("default").as_sql()

    async def titles() -> list[str]:
        return [book.title async for book in make_queryset()]

    # Inside `atomic()`, async queries are logged on this thread's connection (see `djox._running`).
    with transaction.atomic(), CaptureQueriesContext(connection) as captured:
        assert async_to_sync(titles)() == ["Dune"]
        assert async_to_sync(titles)() == ["Dune"]
    assert len(fast_statements()) == 1
    assert [query["sql"] for query in captured.captured_queries] == [sql_with_parameters(sql, parameters)] * 2


@pytest.mark.usefixtures("library")
def test_nothing_runs_when_nothing_can_match() -> None:
    async def ask() -> None:
        assert [book async for book in Book.objects.filter(pages__in=[])] == []
        assert await Book.objects.filter(pages__in=[None]).acount() == 0
        assert await Book.objects.all()[2:2].aexists() is False

    with transaction.atomic(), CaptureQueriesContext(connection) as captured:
        async_to_sync(ask)()
    assert captured.captured_queries == []


@pytest.mark.usefixtures("library")
async def test_numbers_too_big_for_the_column_are_left_to_django() -> None:
    # Django knows nothing matches without asking SQLite (and SQLite couldn't take the number).
    assert await Book.objects.filter(pages=2**70).acount() == 0
    assert await Book.objects.filter(pages__lt=2**70).acount() == 4
    assert fast_statements() == []


# Querysets the fast lane leaves to Django, which still gives the right answer.
DJANGO_QUERYSETS: dict[str, MakeQuerySet] = {
    "Q": lambda: Book.objects.filter(Q(pages__gt=300) | Q(title="Persuasion")),
    "exclude": lambda: Book.objects.exclude(in_print=True),
    "F": lambda: Book.objects.filter(pages__gt=F("id")),
    "a join": lambda: Book.objects.filter(author__name="Jane Austen"),
    "icontains": lambda: Book.objects.filter(title__icontains="dune"),
    "select_related": lambda: Book.objects.select_related("author").filter(pages__gt=300),
    "a string for an integer": lambda: Book.objects.filter(pages="412"),
    "only": lambda: Book.objects.only("title"),
}


@pytest.mark.parametrize("make_queryset", DJANGO_QUERYSETS.values(), ids=DJANGO_QUERYSETS.keys())
@pytest.mark.usefixtures("library")
async def test_other_querysets_go_to_django(make_queryset: MakeQuerySet) -> None:
    books = [book async for book in make_queryset().order_by("pk")]
    expected = await sync_to_async(lambda: list(make_queryset().order_by("pk")))()
    assert [book.pk for book in books] == [book.pk for book in expected]
    assert fast_statements() == []


def test_errors_are_raised_where_django_raises_them() -> None:
    # Django checks names and values when `filter()` runs, not when the query does.
    with pytest.raises(ValueError, match="expected a number"):
        Book.objects.filter(pages="many")
    with pytest.raises(Exception, match="Cannot resolve keyword 'no_such_field'"):
        Book.objects.filter(no_such_field=1)  # type: ignore[misc]  # a wrong name, on purpose
    with pytest.raises(TypeError, match="Cannot filter a query once a slice has been taken"):
        Book.objects.all()[:2].filter(pages=1)


@pytest.mark.usefixtures("library")
async def test_changing_a_list_after_filter_changes_nothing() -> None:
    titles = ["Emma"]
    queryset = Book.objects.filter(title__in=titles)
    titles.append("Dune")
    assert await queryset.acount() == 1
    assert await sync_to_async(lambda: queryset.all().count())() == 1, "nor in Django's Query built from the note"


@pytest.mark.usefixtures("library")
async def test_models_with_init_receivers_are_built_by_django() -> None:
    initialized: list[Book] = []

    def receiver(sender: type[Book], instance: Book, **kwargs: Any) -> None:
        initialized.append(instance)

    post_init.connect(receiver, sender=Book)
    try:
        books = [book async for book in Book.objects.filter(pages__gt=300)]
    finally:
        post_init.disconnect(receiver, sender=Book)
    assert len(books) == 2
    assert initialized == books, "Django's __init__ ran, and sent post_init"


@pytest.mark.usefixtures("library")
async def test_many_queries_of_one_shape_at_once() -> None:
    titles = ["Dune", "Dune Messiah", "Emma", "Persuasion"] * 25
    books = await asyncio.gather(*(Book.objects.aget(title=title) for title in titles))
    assert [book.title for book in books] == titles
    assert len(fast_statements()) == 1


@pytest.mark.usefixtures("library")
async def test_aiterator_and_acontains() -> None:
    assert [book.title async for book in Book.objects.order_by("title").aiterator()][:2] == ["Dune", "Dune Messiah"]
    emma = await Book.objects.aget(title="Emma")
    assert await Book.objects.filter(pages__gt=400).acontains(emma) is True
    assert await Book.objects.filter(pages__lt=400).acontains(emma) is False


def test_the_fast_lane_sees_an_open_sync_transaction() -> None:
    async def count() -> int:
        return await Author.objects.filter(name__in=["Inside", "Outside"]).acount()

    with transaction.atomic():
        Author.objects.create(name="Inside")
        assert async_to_sync(count)() == 1, "the uncommitted row is seen"
        transaction.set_rollback(True)
    assert fast_statements(), "it was the fast lane"


@override_settings(DJOX_FAST_LANE=False)
@pytest.mark.usefixtures("library")
async def test_the_fast_lane_can_be_turned_off() -> None:
    queryset: Any = Book.objects.filter(pages__gt=300)
    assert queryset._simple_query is None
    assert await queryset.acount() == 2
    assert fast_statements() == []


def test_sync_code_builds_djangos_query_from_the_note() -> None:
    queryset = Book.objects.filter(pages__gt=300).order_by("-pages")[:1]
    assert str(queryset.query).endswith('ORDER BY "library_book"."pages" DESC LIMIT 1')
    assert list(queryset) == []
