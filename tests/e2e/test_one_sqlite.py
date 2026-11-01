"""Django's sync ORM and djox's async database share one SQLite.

Before djox's ENGINE, Django's sync ORM used the SQLite inside Python's
`sqlite3` module and djox used its own. Two copies of SQLite in one process
can't see each other's locks, so writes from both sides at once could get
lost without any error. With `"ENGINE": "djox.backends.sqlite3"` both sides
run on djox's copy, so they take turns properly.
"""

import asyncio
import decimal
from pathlib import Path

import pytest
from asgiref.sync import sync_to_async
from django.db import connection

import djox
from tests.e2e.library.models import Author, Book

WRITES_PER_SIDE = 200


def add_book_with_django(number: int, author: Author) -> None:
    Book.objects.create(
        title=f"Django {number}",
        pages=number,
        price=decimal.Decimal("1.00"),
        published="2024-01-01T00:00:00Z",
        author=author,
    )


async def add_book_with_djox(database: djox.Database, number: int, author: Author) -> None:
    await database.execute(
        "INSERT INTO library_book (title, pages, price, published, in_print, author_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        [f"djox {number}", number, "1.00", "2024-01-01 00:00:00", True, author.pk],
    )


@pytest.mark.django_db(transaction=True)
async def test_sync_and_async_writes_at_once_are_all_kept() -> None:
    author = await Author.objects.acreate(name="Many hands")
    database = djox.connections["default"]
    # Django's sync ORM on several threads (each with its own connection),
    # djox's async writer on its own thread, all writing at the same time.
    write_with_django = sync_to_async(add_book_with_django, thread_sensitive=False)

    await asyncio.gather(
        *(write_with_django(number, author) for number in range(WRITES_PER_SIDE)),
        *(add_book_with_djox(database, number, author) for number in range(WRITES_PER_SIDE)),
    )

    assert await Book.objects.acount() == 2 * WRITES_PER_SIDE
    assert await database.fetch_one("SELECT count(*) FROM library_book") == (2 * WRITES_PER_SIDE,)


@pytest.mark.django_db(transaction=True)
async def test_each_side_reads_what_the_other_wrote() -> None:
    database = djox.connections["default"]
    author = await Author.objects.acreate(name="Frank Herbert")
    await add_book_with_djox(database, 412, author)

    book = await Book.objects.aget(author=author)
    assert (book.title, book.pages, book.in_print) == ("djox 412", 412, True)
    assert await database.fetch_all("SELECT name FROM library_author") == [("Frank Herbert",)]


@pytest.mark.django_db(transaction=True)
async def test_the_async_database_knows_djangos_functions() -> None:
    database = djox.connections["default"]
    year = await database.fetch_one("SELECT django_date_extract('year', '1965-08-01')")
    assert year == (1965,)


@pytest.mark.django_db
def test_djox_connections_follow_djangos_settings() -> None:
    database = djox.connections["default"]
    assert database is djox.connections["default"], "one async database per alias"
    assert repr(database) == f"Database.sqlite({connection.settings_dict['NAME']!r})"
    assert Path(connection.settings_dict["NAME"]).name == "test_djox-tests.sqlite3"
