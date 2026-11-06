# mypy: disable-error-code="misc"
# (django-stubs reads a model with generic fields like `ArrayField` as "generic variables via class")
"""Postgres-only fields (arrays, ranges, `hstore`) and `XOR` filters, read and
written by djox's async methods, which run on djox's own Postgres driver.

Django's SQLite backend has no such fields, so the field tests are skipped there.
"""

import datetime
from collections.abc import Iterator

import pytest
from asgiref.sync import sync_to_async
from django.contrib.postgres.fields import ArrayField, DateRangeField, HStoreField, IntegerRangeField
from django.db import connection, models
from django.db.models import Q
from psycopg.types.range import Range

import djox
from tests.e2e.library.models import Author, Book

pytestmark = pytest.mark.django_db(transaction=True)

on_postgres = pytest.mark.skipif(connection.vendor != "postgresql", reason="Postgres fields")


class Shelf(models.Model):
    """A table made by the tests with raw SQL, so the library app's migrations stay SQLite-friendly."""

    tags = ArrayField(models.CharField(max_length=20))
    grid = ArrayField(ArrayField(models.IntegerField()), null=True)
    pages = IntegerRangeField(null=True)
    open_days = DateRangeField(null=True)
    notes = HStoreField(null=True)

    objects = djox.Manager()

    class Meta:
        app_label = "library"
        db_table = "shelf_with_postgres_fields"
        managed = False


def set_notes(shelf_id: int, notes: str) -> None:
    """Store an `hstore` in its text form with raw SQL: Django's sync side only
    adapts a `dict` once `django.contrib.postgres` is installed, which the tests don't need."""
    with connection.cursor() as cursor:
        cursor.execute("UPDATE shelf_with_postgres_fields SET notes = %s::hstore WHERE id = %s", [notes, shelf_id])


@pytest.fixture
def shelf_table() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS hstore")
        cursor.execute("DROP TABLE IF EXISTS shelf_with_postgres_fields")
        cursor.execute(
            "CREATE TABLE shelf_with_postgres_fields (id serial PRIMARY KEY, tags varchar(20)[] NOT NULL, "
            "grid integer[][], pages int4range, open_days daterange, notes hstore)"
        )
    yield
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE shelf_with_postgres_fields")


@on_postgres
async def test_async_methods_read_arrays_ranges_and_hstore(shelf_table: None) -> None:
    shelf = await Shelf.objects.acreate(
        tags=["fiction", "classic"],
        grid=[[1, 2], [3, 4]],
        pages=Range(100, 500),
        open_days=Range(datetime.date(2026, 1, 1), datetime.date(2026, 6, 1)),
    )
    await sync_to_async(set_notes)(shelf.pk, '"owner"=>"Dani", "empty"=>NULL')
    shelf = await Shelf.objects.aget()
    assert shelf.tags == ["fiction", "classic"]
    assert shelf.grid == [[1, 2], [3, 4]]
    assert shelf.pages == Range(100, 500)
    assert shelf.open_days == Range(datetime.date(2026, 1, 1), datetime.date(2026, 6, 1))
    assert shelf.notes == {"owner": "Dani", "empty": None}


@on_postgres
async def test_async_filters_send_arrays_ranges_and_hstore(shelf_table: None) -> None:
    first = await Shelf.objects.acreate(tags=["fiction"], pages=Range(1, 50))
    second = await Shelf.objects.acreate(tags=["history", "fiction"], pages=Range(40, 400))
    await sync_to_async(set_notes)(first.pk, '"owner"=>"Dani"')
    await sync_to_async(set_notes)(second.pk, '"owner"=>"Sam"')
    await Shelf.objects.acreate(tags=[], pages=Range(empty=True))

    assert await Shelf.objects.filter(tags__contains=["fiction"]).acount() == 2
    assert await Shelf.objects.filter(tags__len=2).acount() == 1
    assert await Shelf.objects.filter(pages__overlap=Range(45, 46)).acount() == 2
    assert await Shelf.objects.filter(pages__contains=100).acount() == 1
    assert await Shelf.objects.filter(pages__isempty=True).acount() == 1
    assert await Shelf.objects.filter(notes__owner="Sam").acount() == 1
    assert await Shelf.objects.filter(notes__has_key="owner").acount() == 2
    assert await Shelf.objects.filter(notes__contains={"owner": "Dani"}).acount() == 1


@on_postgres
async def test_values_lists_of_arrays_and_ranges(shelf_table: None) -> None:
    await Shelf.objects.acreate(tags=["a", "b"], pages=Range(1, 5))
    rows = [row async for row in Shelf.objects.values_list("tags", "pages")]
    assert rows == [(["a", "b"], Range(1, 5))]


async def books() -> list[Book]:
    author = await Author.objects.acreate(name="Frank Herbert")
    titles = {
        "Long and in print": (900, True),
        "Long, out of print": (900, False),
        "Short and in print": (100, True),
        "Short, out of print": (100, False),
    }
    return [
        await Book.objects.acreate(
            title=title,
            pages=pages,
            price=10,
            published=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            in_print=in_print,
            author=author,
        )
        for title, (pages, in_print) in titles.items()
    ]


async def test_an_xor_filter_finds_what_one_condition_matches() -> None:
    await books()
    found = Book.objects.filter(Q(pages__gt=500) ^ Q(in_print=True)).order_by("title").values_list("title", flat=True)
    # Long or in print, but not both.
    assert [title async for title in found] == ["Long, out of print", "Short and in print"]


async def test_an_xor_of_three_conditions_matches_an_odd_number_of_them() -> None:
    await books()
    odd = Q(pages__gt=500) ^ Q(in_print=True) ^ Q(title__startswith="Long")
    found = Book.objects.filter(odd).order_by("title").values_list("title", flat=True)
    assert [title async for title in found] == ["Long and in print", "Short and in print"]
    assert await Book.objects.filter(odd).acount() == 2
