"""Django's methods that need a transaction, on djox's own threads.

`aget_or_create()`, `abulk_create()`, `Model.asave()`... run Django's sync
method unchanged, on a thread from djox's pool instead of Django's single
"sync thread". Inside a transaction they run on its thread and join it.
"""

import asyncio
import datetime
import decimal
import threading
from typing import Any

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import transaction
from django.db.models.signals import pre_save

import djox
from tests.e2e.library.models import Author, Book

PUBLISHED = datetime.datetime(1965, 8, 1, 12, 0, tzinfo=datetime.UTC)

pytestmark = pytest.mark.django_db(transaction=True)


class ChangedMyMindError(Exception):
    """Raised by the tests to leave a block with an error."""


def book(title: str, author: Author, pages: int = 100) -> Book:
    return Book(title=title, pages=pages, price=decimal.Decimal("9.99"), published=PUBLISHED, author=author)


# ---------------------------------------------------------------- QuerySet methods


async def test_aget_or_create_creates_once_then_gets() -> None:
    author, created = await Author.objects.aget_or_create(name="Octavia E. Butler")
    assert created is True
    again, created = await Author.objects.aget_or_create(name="Octavia E. Butler")
    assert (again.pk, created) == (author.pk, False)


async def test_get_or_create_at_once_creates_one_row() -> None:
    # Writers take turns, so twenty tasks asking at once create one author.
    results = await asyncio.gather(*(Author.objects.aget_or_create(name="Ursula K. Le Guin") for _ in range(20)))
    assert [created for _, created in results].count(True) == 1
    assert await Author.objects.acount() == 1


async def test_aupdate_or_create_updates_or_creates() -> None:
    author, created = await Author.objects.aupdate_or_create(name="Jane Austen", defaults={"name": "Jane Austen"})
    assert created is True
    updated, created = await Author.objects.aupdate_or_create(name="Jane Austen", defaults={"name": "J. Austen"})
    assert (updated.pk, updated.name, created) == (author.pk, "J. Austen", False)


async def test_abulk_create_and_abulk_update() -> None:
    author = await Author.objects.acreate(name="Frank Herbert")
    books = await Book.objects.abulk_create([book("Dune", author), book("Dune Messiah", author)])
    assert all(saved.pk is not None for saved in books)
    for saved in books:
        saved.pages = 500
    assert await Book.objects.abulk_update(books, ["pages"]) == 2
    assert [pages async for pages in Book.objects.values_list("pages", flat=True)] == [500, 500]


async def test_ain_bulk_maps_ids_to_objects() -> None:
    first = await Author.objects.acreate(name="First")
    second = await Author.objects.acreate(name="Second")
    found = await Author.objects.ain_bulk([first.pk, second.pk])
    assert {pk: author.name for pk, author in found.items()} == {first.pk: "First", second.pk: "Second"}


# ---------------------------------------------------------------- djox.Model


async def test_asave_inserts_then_updates() -> None:
    author = Author(name="Toni Morrison")
    await author.asave()
    assert author.pk is not None
    author.name = "Toni M."
    await author.asave(update_fields=["name"])
    assert (await Author.objects.aget(pk=author.pk)).name == "Toni M."


async def test_adelete_and_arefresh_from_db() -> None:
    author = await Author.objects.acreate(name="Kept")
    await Author.objects.filter(pk=author.pk).aupdate(name="Renamed")
    await author.arefresh_from_db()
    assert author.name == "Renamed"
    assert await author.adelete() == (1, {"library.Author": 1})
    assert await Author.objects.acount() == 0


async def test_django_methods_run_on_djoxs_threads() -> None:
    thread_names: list[str] = []

    def note_thread(sender: type[Author], **kwargs: Any) -> None:
        thread_names.append(threading.current_thread().name)

    pre_save.connect(note_thread, sender=Author)
    try:
        await Author(name="Somewhere").asave()
        await Author.objects.aget_or_create(name="Elsewhere")
    finally:
        pre_save.disconnect(note_thread, sender=Author)
    assert len(thread_names) == 2
    assert all(name.startswith("djox") for name in thread_names), thread_names


async def test_they_dont_wait_for_djangos_sync_thread() -> None:
    release = threading.Event()
    busy = asyncio.ensure_future(sync_to_async(release.wait, thread_sensitive=True)())
    try:
        async with asyncio.timeout(5):
            author, _ = await Author.objects.aget_or_create(name="Not waiting")
            await author.asave()
    finally:
        release.set()
        await busy


# ---------------------------------------------------------------- inside a transaction


async def test_inside_djox_atomic_they_join_the_block() -> None:
    @djox.atomic
    async def save_and_change_my_mind() -> None:
        await Author(name="Rolled back").asave()
        await Author.objects.aget_or_create(name="Rolled back too")
        assert await Author.objects.acount() == 2
        raise ChangedMyMindError

    with pytest.raises(ChangedMyMindError):
        await save_and_change_my_mind()
    assert await Author.objects.acount() == 0


def test_inside_a_sync_transaction_they_join_it() -> None:
    async def get_or_create_twice() -> list[bool]:
        _, first = await Author.objects.aget_or_create(name="Inside")
        _, second = await Author.objects.aget_or_create(name="Inside")
        return [first, second]

    with transaction.atomic():
        assert async_to_sync(get_or_create_twice)() == [True, False]
        transaction.set_rollback(True)
    assert Author.objects.count() == 0
