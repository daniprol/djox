"""djox runs SQL written by Django's own compiler.

This is the path the Django-style API will take: Django writes the SQL (pure
Python, no database access), djox runs it, and Django's converters and
`from_db()` turn the rows back into model instances.

Django's own SQLite connection never opens these files: djox and Python's
`sqlite3` bundle separate copies of SQLite, which can't see each other's locks.
"""

import datetime
import decimal
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import cast

import pytest
from django.db import connection as django_connection
from django.db.models import QuerySet
from django.db.models.query import get_related_populators

import djox
from tests.e2e.library.models import Author, Book

PUBLISHED = datetime.datetime(1965, 8, 1, 12, 0, tzinfo=datetime.UTC)

# The tables Django's migrations would create for `tests.library`.
CREATE_TABLES = [
    "CREATE TABLE library_author (id integer NOT NULL PRIMARY KEY AUTOINCREMENT, name varchar(100) NOT NULL)",
    """CREATE TABLE library_book (
        id integer NOT NULL PRIMARY KEY AUTOINCREMENT,
        title varchar(200) NOT NULL,
        pages integer NOT NULL,
        price decimal NOT NULL,
        published datetime NOT NULL,
        in_print bool NOT NULL,
        author_id bigint NOT NULL REFERENCES library_author (id)
    )""",
]


@pytest.fixture
async def library(tmp_path: Path) -> AsyncIterator[djox.Database]:
    """A database with one author and two books, written by djox."""
    with djox.Database.sqlite(tmp_path / "library.sqlite3") as database:
        for sql in CREATE_TABLES:
            await database.execute(sql)
        author = await database.execute("INSERT INTO library_author (name) VALUES (%s)", ["Frank Herbert"])
        await database.execute_many(
            "INSERT INTO library_book (title, pages, price, published, in_print, author_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [
                ["Dune", 412, decimal.Decimal("9.99"), PUBLISHED, True, author.last_insert_id],
                ["Dune Messiah", 256, decimal.Decimal("8.50"), PUBLISHED, False, author.last_insert_id],
            ],
        )
        yield database


async def fetch_models(database: djox.Database, queryset: QuerySet) -> list:
    """Run a QuerySet on djox, the way the Django-style API will."""
    compiler = queryset.query.get_compiler(using=queryset.db)
    sql, parameters = compiler.as_sql()
    rows = await database.fetch_all(sql, parameters)

    select, klass_info = compiler.select, compiler.klass_info
    first, last = klass_info["select_fields"][0], klass_info["select_fields"][-1] + 1
    field_names = [column[0].target.attname for column in select[first:last]]
    # Django 6.1 added "fetch modes": `from_db()` and the populators take one more argument.
    # The signature depends on the installed Django, so it is called through an untyped name.
    populators_for = cast("Callable[..., list]", get_related_populators)
    if hasattr(queryset, "_fetch_mode"):
        fetch_mode = {"fetch_mode": queryset._fetch_mode}
        related_populators = populators_for(klass_info, select, queryset.db, queryset._fetch_mode)
    else:
        fetch_mode = {}
        related_populators = populators_for(klass_info, select, queryset.db)

    model = queryset.model
    assert model is not None
    instances = []
    for row in compiler.results_iter(results=[rows]):
        instance = model.from_db(queryset.db, field_names, row[first:last], **fetch_mode)
        for populator in related_populators:
            populator.populate(row, instance)
        instances.append(instance)
    return instances


async def test_runs_a_filter_from_djangos_compiler(library: djox.Database) -> None:
    books = await fetch_models(library, Book.objects.filter(pages__gt=300, title__startswith="Du"))

    assert [book.title for book in books] == ["Dune"]
    book = books[0]
    assert book.price == decimal.Decimal("9.99")
    assert book.published == PUBLISHED
    assert book.in_print is True


async def test_runs_select_related_from_djangos_compiler(library: djox.Database) -> None:
    books = await fetch_models(library, Book.objects.select_related("author").order_by("-pages"))

    assert [(book.title, book.in_print, book.author.name) for book in books] == [
        ("Dune", True, "Frank Herbert"),
        ("Dune Messiah", False, "Frank Herbert"),
    ]


async def test_runs_aggregates_from_djangos_compiler(library: djox.Database) -> None:
    queryset = Author.objects.filter(books__in_print=True).values_list("name")
    sql, parameters = queryset.query.get_compiler(using="default").as_sql()
    assert await library.fetch_all(sql, parameters) == [("Frank Herbert",)]


def test_compiling_sql_never_opens_djangos_connection() -> None:
    Book.objects.filter(pages__gt=300).select_related("author").query.get_compiler(using="default").as_sql()
    assert django_connection.connection is None
