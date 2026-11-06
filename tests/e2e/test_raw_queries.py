"""`Manager.araw()`: Django's `raw()`, awaited on djox.

Each test checks that `araw()` gives what Django's sync `raw()` gives, while
the SQL runs on djox's async database (`transaction=True`: no test
transaction, so queries take djox's async path).
"""

import datetime
import decimal
from collections.abc import Iterator
from typing import Any

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import transaction

import djox
from tests.e2e.library.models import Author, Book

PUBLISHED = datetime.datetime(1965, 8, 1, 12, 0, tzinfo=datetime.UTC)
ALL_BOOKS = "SELECT * FROM library_book ORDER BY id"

pytestmark = pytest.mark.django_db(transaction=True)


class StatementRecorder:
    """Hooks that note the SQL of every statement."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def before_query(self, query: djox.Query) -> None:
        self.sql.append(query.sql)


@pytest.fixture
def recorder() -> Iterator[StatementRecorder]:
    recorder = StatementRecorder()
    djox.add_hooks(recorder)
    yield recorder
    djox.remove_hooks(recorder)


def fields_of(book: Book) -> dict[str, Any]:
    """A book's attributes, without Django's bookkeeping (`_state`, which differs by object)."""
    return {name: value for name, value in book.__dict__.items() if name != "_state"}


@pytest.fixture
def library() -> None:
    """One author and three books, written with Django's sync ORM."""
    herbert = Author.objects.create(name="Frank Herbert")
    for title, pages in [("Dune", 412), ("Dune Messiah", 256), ("Children of Dune", 444)]:
        Book.objects.create(
            title=title, pages=pages, price=decimal.Decimal("9.99"), published=PUBLISHED, author=herbert
        )


@pytest.mark.usefixtures("library")
async def test_araw_can_be_awaited_for_model_instances() -> None:
    books = await Book.objects.araw(ALL_BOOKS)

    assert [book.title for book in books] == ["Dune", "Dune Messiah", "Children of Dune"]
    assert all(isinstance(book, Book) for book in books)
    assert books[0].price == decimal.Decimal("9.99")  # converted, like Django does
    assert books[0].published == PUBLISHED
    assert books[0].in_print is True
    assert books[0]._state.db == "default"


@pytest.mark.usefixtures("library")
async def test_araw_can_be_used_with_async_for() -> None:
    titles = [book.title async for book in Book.objects.araw(ALL_BOOKS)]

    assert titles == ["Dune", "Dune Messiah", "Children of Dune"]


@pytest.mark.usefixtures("library")
async def test_araw_gives_the_same_instances_as_djangos_raw() -> None:
    sql = "SELECT * FROM library_book WHERE pages > %s ORDER BY id"
    from_django = [fields_of(book) for book in await sync_to_async(lambda: list(Book.objects.raw(sql, [300])))()]

    from_djox = [fields_of(book) for book in await Book.objects.araw(sql, [300])]

    assert from_djox == from_django
    assert len(from_djox) == 2


@pytest.mark.usefixtures("library")
async def test_araw_adds_columns_that_are_not_fields_as_attributes() -> None:
    books = await Book.objects.araw("SELECT id, title, pages * 2 AS double_pages FROM library_book ORDER BY id")

    assert [book.double_pages for book in books] == [824, 512, 888]


@pytest.mark.usefixtures("library")
async def test_araw_translations_map_columns_to_fields() -> None:
    books = await Book.objects.araw(
        "SELECT id, title AS name, pages, price, published, in_print, author_id FROM library_book ORDER BY id",
        translations={"name": "title"},
    )

    assert books[0].title == "Dune"


@pytest.mark.usefixtures("library")
async def test_araw_leaves_fields_missing_from_the_query_deferred() -> None:
    books = await Book.objects.araw("SELECT id, title FROM library_book ORDER BY id")

    assert "pages" in books[0].get_deferred_fields()
    assert (await Book.objects.aget(pk=books[0].pk)).pages == 412


async def test_araw_needs_the_primary_key() -> None:
    with pytest.raises(Exception, match="Raw query must include the primary key"):
        await Book.objects.araw("SELECT title FROM library_book")


@pytest.mark.usefixtures("library")
async def test_araw_runs_the_sql_once_and_keeps_the_instances(recorder: StatementRecorder) -> None:
    queryset = Book.objects.araw(ALL_BOOKS)

    first = await queryset
    second = await queryset

    assert first == second
    assert recorder.sql == [ALL_BOOKS]


@pytest.mark.usefixtures("library")
async def test_araw_prefetches_related_objects() -> None:
    books = await Book.objects.araw(ALL_BOOKS).prefetch_related("author")

    assert [book.author.name for book in books] == ["Frank Herbert"] * 3


@pytest.mark.usefixtures("library")
async def test_araw_named_parameters_fall_back_to_djangos_raw() -> None:
    books = await Book.objects.araw("SELECT * FROM library_book WHERE title = %(title)s", {"title": "Dune"})

    assert [book.title for book in books] == ["Dune"]


@pytest.mark.usefixtures("library")
async def test_araw_sees_the_writes_of_an_open_djox_atomic_block() -> None:
    async with djox.atomic():
        author = await Author.objects.aget(name="Frank Herbert")
        await Book.objects.acreate(title="God Emperor of Dune", pages=423, price=1, published=PUBLISHED, author=author)

        books = await Book.objects.araw("SELECT * FROM library_book WHERE title LIKE %s", ["God%"])

    assert [book.title for book in books] == ["God Emperor of Dune"]


@pytest.mark.usefixtures("library")
async def test_araw_runs_the_hooks(recorder: StatementRecorder) -> None:
    await Book.objects.araw("SELECT * FROM library_book WHERE pages > %s", [300])

    assert recorder.sql == ["SELECT * FROM library_book WHERE pages > %s"]


@pytest.mark.usefixtures("library")
def test_araw_sees_the_writes_of_an_open_sync_transaction() -> None:
    """Like Django's `TestCase`: each test runs inside a sync `atomic()` block."""

    async def titles_like(pattern: str) -> list[str]:
        return [
            book.title async for book in Book.objects.araw("SELECT * FROM library_book WHERE title LIKE %s", [pattern])
        ]

    with transaction.atomic():
        herbert = Author.objects.get(name="Frank Herbert")
        Book.objects.create(title="God Emperor of Dune", pages=423, price=1, published=PUBLISHED, author=herbert)

        assert async_to_sync(titles_like)("God%") == ["God Emperor of Dune"]
