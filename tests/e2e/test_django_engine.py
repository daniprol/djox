"""Django on djox's ENGINE: the ORM, raw cursors, Django's SQL functions,
transactions and migrations, all on the SQLite bundled in djox."""

import datetime
import decimal
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, connections, transaction
from django.db.models import Count, F, StdDev
from django.db.models.functions import ExtractYear, Power, TruncMonth

import djox
from djox.backends.sqlite3.base import DatabaseWrapper
from tests.e2e.library.models import Author, Book

PUBLISHED = datetime.datetime(1965, 8, 1, 12, 0, tzinfo=datetime.UTC)


def add_dune(title: str = "Dune", pages: int = 412) -> Book:
    author, _ = Author.objects.get_or_create(name="Frank Herbert")
    return Book.objects.create(
        title=title, pages=pages, price=decimal.Decimal("9.99"), published=PUBLISHED, author=author
    )


@pytest.mark.django_db
def test_djangos_connection_is_a_djox_connection() -> None:
    connection.ensure_connection()
    assert isinstance(connection.connection, djox.Connection)
    assert connection.vendor == "sqlite"
    assert isinstance(connections["default"], DatabaseWrapper)
    assert connection.get_database_version() >= (3, 54)


@pytest.mark.django_db
def test_the_test_database_is_a_wal_file() -> None:
    assert Path(connection.settings_dict["NAME"]).name == "test_djox-tests.sqlite3"
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode")
        assert cursor.fetchone() == ("wal",)


@pytest.mark.django_db
def test_the_orm_reads_and_writes() -> None:
    dune = add_dune()
    add_dune("Dune Messiah", 256)

    assert Book.objects.count() == 2
    assert Book.objects.get(pk=dune.pk).published == PUBLISHED
    assert list(Book.objects.filter(pages__gt=300).values_list("title", flat=True)) == ["Dune"]
    assert Book.objects.filter(title__startswith="Dune").update(pages=F("pages") + 1) == 2
    assert [book.author.name for book in Book.objects.select_related("author")] == ["Frank Herbert"] * 2
    assert Author.objects.annotate(book_count=Count("books")).get().book_count == 2

    deleted, _ = Author.objects.all().delete()
    assert deleted == 3  # the author and, by cascade, both books


@pytest.mark.django_db
def test_djangos_sql_functions_run_on_djox() -> None:
    add_dune()
    add_dune("Dune Messiah", 256)

    # Extract and Trunc call `django_datetime_extract` and `django_datetime_trunc`.
    book = Book.objects.annotate(year=ExtractYear("published"), month=TruncMonth("published")).first()
    assert book is not None
    assert (book.year, book.month) == (1965, datetime.datetime(1965, 8, 1, tzinfo=datetime.UTC))
    assert Book.objects.filter(published__week_day=1).count() == 2  # a Sunday
    # `regex` calls Django's `REGEXP`, `StdDev` its `STDDEV_POP` aggregate.
    assert Book.objects.filter(title__regex=r"^Dune$").count() == 1
    assert Book.objects.aggregate(spread=StdDev("pages"))["spread"] == 78.0
    # `POWER` is built into djox's SQLite.
    assert Book.objects.annotate(squared=Power("pages", 2)).values_list("squared", flat=True)[0] == 412.0**2


@pytest.mark.django_db
def test_raw_cursors_use_djangos_placeholders() -> None:
    add_dune()
    with connection.cursor() as cursor:
        cursor.execute("SELECT title FROM library_book WHERE pages > %s", [400])
        assert cursor.fetchall() == [("Dune",)]
        cursor.execute("SELECT title FROM library_book WHERE pages > %(least)s", {"least": 400})
        assert cursor.fetchall() == [("Dune",)]
        # Without parameters the SQL runs as written, so `%` needs no escaping.
        cursor.execute("SELECT count(*) FROM library_book WHERE title LIKE 'D%'")
        assert cursor.fetchone() == (1,)


def add_dune_then_fail() -> None:
    with transaction.atomic():
        add_dune()
        raise ValueError("changed my mind")


def add_a_book_by_a_missing_author() -> None:
    with transaction.atomic():
        Book.objects.create(title="Nobody's", pages=1, price=1, published=PUBLISHED, author_id=999)
        # SQLite checks Django's foreign keys at COMMIT; ask now instead.
        connection.check_constraints()


@pytest.mark.django_db(transaction=True)
def test_atomic_rolls_back_on_an_exception() -> None:
    with pytest.raises(ValueError, match="changed my mind"):
        add_dune_then_fail()
    assert Book.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_a_savepoint_rolls_back_only_its_own_writes() -> None:
    with transaction.atomic():
        add_dune()
        with pytest.raises(IntegrityError):
            add_a_book_by_a_missing_author()
        add_dune("Dune Messiah", 256)
    assert sorted(Book.objects.values_list("title", flat=True)) == ["Dune", "Dune Messiah"]


@pytest.mark.django_db(transaction=True)
def test_set_autocommit_false_keeps_writes_until_commit() -> None:
    transaction.set_autocommit(False)
    try:
        add_dune()
        transaction.rollback()
        assert Book.objects.count() == 0
        add_dune()
        transaction.commit()
    finally:
        transaction.set_autocommit(True)
    assert Book.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_atomic_takes_the_write_lock_at_begin() -> None:
    queries: list[str] = []

    def remember(execute, sql, params, many, context):  # type: ignore[no-untyped-def]  # Django's hook signature
        queries.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(remember), transaction.atomic():
        add_dune()
    assert queries[0] == "BEGIN IMMEDIATE"


@pytest.mark.django_db(transaction=True)
def test_migrations_run_backwards_and_forwards() -> None:
    call_command("migrate", "library", "zero", verbosity=0)
    assert "library_book" not in connection.introspection.table_names()
    call_command("migrate", "library", verbosity=0)
    assert "library_book" in connection.introspection.table_names()
