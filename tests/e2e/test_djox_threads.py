"""Django's methods that need a transaction, on djox's own threads.

`aupdate_or_create()`, `Model.asave()`, and the methods djox runs itself
when they need Django's code (`aget_or_create()` with sync signal
receivers...) run Django's sync method unchanged, on a thread from djox's
pool instead of Django's single "sync thread". Inside a transaction they
run on its thread and join it.
"""

import asyncio
import datetime
import decimal
import threading
from typing import Any

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.core.signals import request_finished
from django.db import connection, transaction
from django.db.models.signals import pre_save

import djox
from djox import _threads
from tests.e2e.library.models import Author, Book

PUBLISHED = datetime.datetime(1965, 8, 1, 12, 0, tzinfo=datetime.UTC)

pytestmark = pytest.mark.django_db(transaction=True)


class ChangedMyMindError(Exception):
    """Raised by the tests to leave a block with an error."""


def book(title: str, author: Author, pages: int = 100) -> Book:
    return Book(title=title, pages=pages, price=decimal.Decimal("9.99"), published=PUBLISHED, author=author)


# ---------------------------------------------------------------- QuerySet methods


async def test_aupdate_or_create_updates_or_creates() -> None:
    author, created = await Author.objects.aupdate_or_create(name="Jane Austen", defaults={"name": "Jane Austen"})
    assert created is True
    updated, created = await Author.objects.aupdate_or_create(name="Jane Austen", defaults={"name": "J. Austen"})
    assert (updated.pk, updated.name, created) == (author.pk, "J. Austen", False)


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


async def test_many_blocks_at_once_share_a_few_threads() -> None:
    # Each pooled thread holds a connection: on Postgres, a hundred blocks at
    # once must not open a hundred connections.
    thread_names: set[str] = set()

    async def add_author(number: int) -> None:
        async with djox.atomic():
            author = await Author.objects.acreate(name=f"Author {number}")
            await sync_to_async(lambda: thread_names.add(threading.current_thread().name), thread_sensitive=True)()
            await author.asave()

    await asyncio.gather(*(add_author(number) for number in range(50)))

    assert await Author.objects.acount() == 50
    assert len(thread_names) <= _threads.MAX_WRITING_THREADS


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


# ---------------------------------------------------------------- related managers


async def test_related_managers_run_on_djoxs_threads() -> None:
    # `author.books` is a related manager Django builds from `djox.Manager`.
    author = await Author.objects.acreate(name="Frank Herbert")
    thread_names: list[str] = []

    def note_thread(sender: type[Book], **kwargs: Any) -> None:
        thread_names.append(threading.current_thread().name)

    pre_save.connect(note_thread, sender=Book)
    try:
        dune = await author.books.acreate(title="Dune", pages=412, price=decimal.Decimal("9.99"), published=PUBLISHED)
    finally:
        pre_save.disconnect(note_thread, sender=Book)
    assert dune.author_id == author.pk
    assert thread_names[0].startswith("djox"), thread_names


# ---------------------------------------------------------------- connection upkeep


async def test_a_pooled_threads_connection_is_closed_after_each_request() -> None:
    # `CONN_MAX_AGE = 0` (Django's default) closes connections after each
    # request; djox's pooled threads follow it, before their next call.
    def this_threads_connection() -> object:
        connection.ensure_connection()
        return connection.connection

    first = await _threads.run_django_method("default", this_threads_connection, writes=False)
    again = await _threads.run_django_method("default", this_threads_connection, writes=False)
    assert again is first, "no request went by: the connection is kept"

    await request_finished.asend(sender=None)
    after_request = await _threads.run_django_method("default", this_threads_connection, writes=False)
    assert after_request is not first
