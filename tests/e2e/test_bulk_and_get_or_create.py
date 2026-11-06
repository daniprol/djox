"""Methods of several statements, awaited on djox: `aget_or_create()`,
`ain_bulk()`, `abulk_create()` and `abulk_update()`, and their sync
versions on the fast lane (`in_bulk()`, `bulk_update()`).

Each test checks one behavior against Django's (with the fast lane off,
`djangos()`), or against the rows in the database.
"""

import asyncio
import contextvars
import datetime
import decimal
from collections.abc import Iterator

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.test.utils import CaptureQueriesContext

import djox
from djox._fast_lane import bulk_updates
from djox._fast_lane.statements import LEFT_TO_DJANGO
from tests.e2e.kinds.models import Tag, Ticket
from tests.e2e.library.models import Author, Book
from tests.e2e.test_fast_lane import PUBLISHED, djangos, fields_of

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def no_statements_yet() -> Iterator[None]:
    """Each test starts with the fast lane knowing no batch `UPDATE` yet."""
    bulk_updates.statements.clear()
    yield
    bulk_updates.statements.clear()


class ChangedMyMindError(Exception):
    """Raised by the tests to leave a block with an error."""


def book(title: str, author: Author, pages: int = 100) -> Book:
    return Book(title=title, pages=pages, price=decimal.Decimal("9.99"), published=PUBLISHED, author=author)


def learned_updates() -> list[str]:
    """The batch `UPDATE`s the fast lane learned (and didn't leave to Django)."""
    return [sql for sql in bulk_updates.statements.values() if sql != LEFT_TO_DJANGO]


async def committed_titles() -> list[str]:
    """The books' titles, read outside any transaction (so only committed rows)."""

    async def read() -> list[str]:
        return [title async for title in Book.objects.order_by("title").values_list("title", flat=True)]

    # A task with an empty context is like another request: it is in no transaction.
    return await asyncio.create_task(read(), context=contextvars.Context())


# ---------------------------------------------------------------- get_or_create()


async def test_aget_or_create_creates_once_then_gets() -> None:
    author, created = await Author.objects.aget_or_create(name="Octavia E. Butler")
    assert created is True
    again, created = await Author.objects.aget_or_create(name="Octavia E. Butler")
    assert (again.pk, created) == (author.pk, False)


async def test_aget_or_create_uses_the_defaults_and_calls_them() -> None:
    author = await Author.objects.acreate(name="Frank Herbert")
    dune, created = await Book.objects.aget_or_create(  # type: ignore[misc]  # django-stubs doesn't know defaults may be functions
        title="Dune",
        defaults={"pages": lambda: 412, "price": decimal.Decimal("9.99"), "published": PUBLISHED, "author": author},
    )
    assert created is True
    assert (await Book.objects.aget(pk=dune.pk)).pages == 412


@pytest.mark.sqlite_only(
    "SQLite's one writer makes get_or_create() safe without a unique constraint; Postgres needs one, as in Django"
)
async def test_get_or_create_at_once_creates_one_row() -> None:
    # Each task looks again inside its block, which holds the write lock.
    results = await asyncio.gather(*(Author.objects.aget_or_create(name="Ursula K. Le Guin") for _ in range(20)))
    assert [created for _, created in results].count(True) == 1
    assert await Author.objects.acount() == 1


async def test_get_or_create_at_once_on_a_unique_column_creates_one_row() -> None:
    results = await asyncio.gather(
        *(Tag.objects.aget_or_create(name="red", defaults={"color": "red"}) for _ in range(20))
    )
    assert [created for _, created in results].count(True) == 1
    assert await Tag.objects.acount() == 1


async def test_a_failed_create_doesnt_spoil_the_block_around_it() -> None:
    # The `INSERT` runs in a savepoint: the error undoes it alone, like in Django.
    author = await Author.objects.acreate(name="Frank Herbert")
    async with djox.atomic():
        await Book.objects.acreate(title="Kept", pages=1, price=1, published=PUBLISHED, author=author)
        with pytest.raises(IntegrityError):
            await Book.objects.aget_or_create(title="Broken", defaults={"pages": None})  # type: ignore[misc]  # on purpose
        await Book.objects.acreate(title="Kept too", pages=1, price=1, published=PUBLISHED, author=author)
    assert await committed_titles() == ["Kept", "Kept too"]


# ---------------------------------------------------------------- in_bulk()


@pytest.fixture
def three_tags() -> None:
    for name in ("red", "green", "blue"):
        Tag.objects.create(name=name, color=name.upper())


@pytest.mark.usefixtures("three_tags")
def test_in_bulk_gives_djangos_objects() -> None:
    def found() -> dict[str, dict[str, object]]:
        return {name: fields_of(tag) for name, tag in Tag.objects.in_bulk(["red", "blue", "pink"]).items()}

    assert found() == djangos(found)
    assert set(found()) == {"red", "blue"}


@pytest.mark.usefixtures("three_tags")
async def test_ain_bulk_gives_djangos_objects() -> None:
    def djangos_answer() -> dict[str, dict[str, object]]:
        return {name: fields_of(tag) for name, tag in Tag.objects.in_bulk(["red", "blue"]).items()}

    found = await Tag.objects.ain_bulk(["red", "blue"])
    assert {name: fields_of(tag) for name, tag in found.items()} == await sync_to_async(djangos)(djangos_answer)


@pytest.mark.usefixtures("three_tags")
async def test_ain_bulk_without_ids_gives_every_object() -> None:
    assert set(await Tag.objects.filter(color__in=["RED", "BLUE"]).ain_bulk()) == {"red", "blue"}


async def test_ain_bulk_by_a_unique_field() -> None:
    first = await Author.objects.acreate(name="First")
    ticket = await Ticket.objects.acreate(title="One", priority=1)
    assert await Author.objects.ain_bulk([first.pk]) == {first.pk: first}
    assert await Ticket.objects.ain_bulk([ticket.pk], field_name="id") == {ticket.pk: ticket}


def test_ain_bulk_of_no_ids_runs_no_query() -> None:
    async def ask() -> None:
        assert await Tag.objects.ain_bulk([]) == {}

    with CaptureQueriesContext(connection) as captured:
        async_to_sync(ask)()
    assert captured.captured_queries == []


@pytest.mark.usefixtures("three_tags")
async def test_ain_bulk_leaves_the_rest_to_django() -> None:
    # A field that isn't unique: Django's error.
    with pytest.raises(ValueError, match="must be a unique field"):
        await Tag.objects.ain_bulk(["RED"], field_name="color")  # type: ignore[misc]  # on purpose
    # A slice: Django's error.
    with pytest.raises(TypeError, match="Cannot use 'limit' or 'offset'"):
        await Tag.objects.all()[:1].ain_bulk(["red"])


# ---------------------------------------------------------------- bulk_create()


async def test_abulk_create_saves_the_objects_like_django() -> None:
    author = await Author.objects.acreate(name="Frank Herbert")
    books = await Book.objects.abulk_create([book("Dune", author), book("Dune Messiah", author)])
    assert all(saved.pk is not None and not saved._state.adding for saved in books)
    for saved in books:
        assert fields_of(saved) == fields_of(await Book.objects.aget(pk=saved.pk))


async def test_abulk_create_gives_ids_from_a_default() -> None:
    tickets = await Ticket.objects.abulk_create([Ticket(title="One", priority=1), Ticket(title="Two", priority=2)])
    assert sorted(await Ticket.objects.ain_bulk([ticket.pk for ticket in tickets])) == sorted(
        ticket.pk for ticket in tickets
    )


async def test_abulk_create_in_batches_is_all_or_nothing() -> None:
    author = await Author.objects.acreate(name="Frank Herbert")
    broken = book("Broken", author)
    broken.published = None  # type: ignore[assignment]  # a missing value, on purpose
    with pytest.raises(IntegrityError):
        await Book.objects.abulk_create([book("Dune", author), broken], batch_size=1)
    assert await committed_titles() == [], "the first batch was rolled back with the second"
    await Book.objects.abulk_create([book(f"Book {number}", author) for number in range(5)], batch_size=2)
    assert len(await committed_titles()) == 5


async def test_abulk_create_ignores_conflicts() -> None:
    await Tag.objects.acreate(name="red", color="RED")
    await Tag.objects.abulk_create(
        [Tag(name="red", color="PINK"), Tag(name="blue", color="BLUE")], ignore_conflicts=True
    )
    assert {tag.name: tag.color async for tag in Tag.objects.all()} == {"red": "RED", "blue": "BLUE"}


async def test_abulk_create_updates_conflicts() -> None:
    await Tag.objects.acreate(name="red", color="RED")
    await Tag.objects.abulk_create(
        [Tag(name="red", color="PINK")], update_conflicts=True, update_fields=["color"], unique_fields=["name"]
    )
    assert {tag.name: tag.color async for tag in Tag.objects.all()} == {"red": "PINK"}


async def test_abulk_create_raises_djangos_errors() -> None:
    with pytest.raises(ValueError, match="Batch size must be a positive integer"):
        await Tag.objects.abulk_create([Tag(name="red")], batch_size=0)
    with pytest.raises(ValueError, match="unsaved related object 'author'"):
        await Book.objects.abulk_create([book("Dune", Author(name="Not saved"))])


async def test_abulk_create_inside_a_block_shares_its_fate() -> None:
    author = await Author.objects.acreate(name="Frank Herbert")

    async def create_and_change_my_mind() -> None:
        async with djox.atomic():
            await Book.objects.abulk_create([book(f"Book {number}", author) for number in range(4)], batch_size=2)
            raise ChangedMyMindError

    with pytest.raises(ChangedMyMindError):
        await create_and_change_my_mind()
    assert await committed_titles() == []


# ---------------------------------------------------------------- bulk_update()


@pytest.fixture
def books() -> list[Book]:
    author = Author.objects.create(name="Frank Herbert")
    return Book.objects.bulk_create([book(f"Book {number}", author, pages=number) for number in range(1, 6)])


def stored_books() -> list[dict[str, object]]:
    return [fields_of(stored) for stored in Book.objects.order_by("pk")]


def test_bulk_update_learns_the_statement_then_reuses_it(books: list[Book]) -> None:
    for saved in books:
        saved.pages += 100
    with CaptureQueriesContext(connection) as captured:
        assert Book.objects.bulk_update(books, ["pages"]) == 5  # Django's way, learned
        assert Book.objects.bulk_update(books, ["pages"]) == 5  # the fast lane's
    assert len(learned_updates()) == 1
    assert captured.captured_queries[0]["sql"] == captured.captured_queries[1]["sql"]
    assert [stored["pages"] for stored in stored_books()] == [101, 102, 103, 104, 105]


def test_bulk_update_writes_djangos_rows(books: list[Book]) -> None:
    # Values of several types, `None`s (written `NULL`) and the same value twice.
    def update() -> list[dict[str, object]]:
        for number, saved in enumerate(books):
            saved.pages = 7
            saved.price = decimal.Decimal("1.50") if number % 2 else 2  # pyrefly: ignore[bad-assignment]  # an int, on purpose
            saved.published = PUBLISHED + datetime.timedelta(days=number)
            saved.in_print = number == 3
        Book.objects.bulk_update(books, ["pages", "price", "published", "in_print"], batch_size=2)
        return stored_books()

    assert update() == djangos(update)
    assert update() == djangos(update)  # the second time, from learned statements
    assert learned_updates()


async def test_abulk_update_learns_the_statement_then_reuses_it(books: list[Book]) -> None:
    for saved in books:
        saved.pages += 100
    assert await Book.objects.abulk_update(books, ["pages"]) == 5
    assert await Book.objects.abulk_update(books, ["pages"]) == 5
    assert len(learned_updates()) == 1
    assert [pages async for pages in Book.objects.order_by("pk").values_list("pages", flat=True)] == [
        101,
        102,
        103,
        104,
        105,
    ]


async def test_abulk_update_takes_expressions_and_repeats_djangos_way(books: list[Book]) -> None:
    books[0].pages = F("pages") + 10  # pyrefly: ignore[bad-assignment]  # an expression
    books[1].pages = 50
    assert await Book.objects.abulk_update([books[0], books[1], books[1]], ["pages"]) == 2
    assert [pages async for pages in Book.objects.order_by("pk").values_list("pages", flat=True)][:2] == [11, 50]
    assert learned_updates() == [], "Django wrote both statements"


async def test_abulk_update_in_batches_is_all_or_nothing(books: list[Book]) -> None:
    books[0].title = "Changed"
    books[4].published = None  # type: ignore[assignment]  # a missing value, on purpose
    with pytest.raises(IntegrityError):
        await Book.objects.abulk_update(books, ["title", "published"], batch_size=2)
    assert "Changed" not in await committed_titles(), "the first batch was rolled back with the last"


async def test_abulk_update_raises_djangos_errors(books: list[Book]) -> None:
    with pytest.raises(ValueError, match="Field names must be given"):
        await Book.objects.abulk_update(books, [])  # type: ignore[misc]  # on purpose
    with pytest.raises(ValueError, match="cannot be used with primary key fields"):
        await Book.objects.abulk_update(books, ["id"])  # type: ignore[misc]  # on purpose
    with pytest.raises(ValueError, match="must have a primary key set"):
        await Book.objects.abulk_update([Book(title="New")], ["title"])


async def test_abulk_update_of_a_filtered_queryset_is_djangos(books: list[Book]) -> None:
    for saved in books:
        saved.pages = 1000
    # Django adds the filter to each batch's `WHERE`: only the books it matches change.
    assert await Book.objects.filter(pages__lte=2).abulk_update(books, ["pages"]) == 2
    assert [pages async for pages in Book.objects.order_by("pk").values_list("pages", flat=True)] == [
        1000,
        1000,
        3,
        4,
        5,
    ]


def test_bulk_update_in_a_sync_transaction_rolls_back_with_it(books: list[Book]) -> None:
    for saved in books:
        saved.pages = 0
    with transaction.atomic():
        Book.objects.bulk_update(books, ["pages"], batch_size=2)
        transaction.set_rollback(True)
    assert [stored["pages"] for stored in stored_books()] == [1, 2, 3, 4, 5]
