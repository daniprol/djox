"""`djox.Manager`: Django's async QuerySet methods, running on djox.

The library models use `objects = djox.Manager()`. Each test checks that an
async method gives exactly what Django's sync method gives, while the SQL
runs on djox's async database (`transaction=True`: no test transaction, so
queries take djox's async path).
"""

import asyncio
import datetime
import decimal
import threading
from typing import Any

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, F, Max, Sum
from django.db.models.signals import post_init, post_save, pre_save
from django.test.utils import CaptureQueriesContext

import djox
from tests.e2e.library.models import Author, Book

PUBLISHED = datetime.datetime(1965, 8, 1, 12, 0, tzinfo=datetime.UTC)

pytestmark = pytest.mark.django_db(transaction=True)


def add_book(title: str, pages: int, author: Author) -> Book:
    return Book.objects.create(
        title=title, pages=pages, price=decimal.Decimal("9.99"), published=PUBLISHED, author=author
    )


@pytest.fixture
def library() -> None:
    """Two authors and three books, written with Django's sync ORM."""
    herbert = Author.objects.create(name="Frank Herbert")
    austen = Author.objects.create(name="Jane Austen")
    add_book("Dune", 412, herbert)
    add_book("Dune Messiah", 256, herbert)
    add_book("Emma", 474, austen)


# ---------------------------------------------------------------- reading


@pytest.mark.usefixtures("library")
async def test_aget_finds_the_one_matching_book() -> None:
    book = await Book.objects.aget(title="Emma")
    assert (book.title, book.pages, book.price, book.published) == ("Emma", 474, decimal.Decimal("9.99"), PUBLISHED)
    assert book._state.adding is False
    assert book._state.db == "default"


@pytest.mark.usefixtures("library")
async def test_aget_raises_djangos_exceptions() -> None:
    with pytest.raises(Book.DoesNotExist, match=r"Book matching query does not exist\."):
        await Book.objects.aget(title="Ulysses")
    with pytest.raises(Book.MultipleObjectsReturned, match=r"it returned 2!"):
        await Book.objects.aget(author__name="Frank Herbert")


@pytest.mark.usefixtures("library")
async def test_async_for_gives_what_a_for_loop_gives() -> None:
    queryset = Book.objects.select_related("author").annotate(double=F("pages") * 2).order_by("-pages")
    books = [book async for book in queryset]
    expected = await sync_to_async(lambda: list(queryset.values_list("title", "author__name", "double")))()
    assert [(book.title, book.author.name, book.double) for book in books] == expected


@pytest.mark.usefixtures("library")
async def test_values_and_values_list_give_dicts_and_tuples() -> None:
    queryset = Book.objects.order_by("title")
    assert [row async for row in queryset.values("title", "author__name")] == [
        {"title": "Dune", "author__name": "Frank Herbert"},
        {"title": "Dune Messiah", "author__name": "Frank Herbert"},
        {"title": "Emma", "author__name": "Jane Austen"},
    ]
    assert [row async for row in queryset.values_list("title", "pages")] == [
        ("Dune", 412),
        ("Dune Messiah", 256),
        ("Emma", 474),
    ]
    assert [title async for title in queryset.values_list("title", flat=True)] == ["Dune", "Dune Messiah", "Emma"]
    named = [row async for row in queryset.values_list("title", named=True)]
    assert named[2].title == "Emma"


@pytest.mark.usefixtures("library")
async def test_aiterator_gives_every_book_without_keeping_them() -> None:
    queryset = Book.objects.order_by("pages")
    titles = [book.title async for book in queryset.aiterator(chunk_size=2)]
    assert titles == ["Dune Messiah", "Dune", "Emma"]
    assert queryset._result_cache is None


@pytest.mark.usefixtures("library")
async def test_aiterator_builds_the_books_one_chunk_at_a_time() -> None:
    built: list[Book] = []

    def count_built(sender: type[Book], instance: Book, **kwargs: Any) -> None:
        built.append(instance)

    post_init.connect(count_built, sender=Book)
    try:
        books = Book.objects.select_related("author").order_by("pages").aiterator(chunk_size=2)
        first = await anext(books)
        assert (first.title, first.author.name) == ("Dune Messiah", "Frank Herbert")
        assert len(built) == 2, "the books of the first chunk only"
        assert [book.title async for book in books] == ["Dune", "Emma"]
    finally:
        post_init.disconnect(count_built, sender=Book)


@pytest.mark.usefixtures("library")
async def test_writes_run_while_aiterator_reads() -> None:
    async for book in Book.objects.order_by("pages").aiterator(chunk_size=1):
        # The stream reads on a connection of its own: the writer is free.
        await Book.objects.filter(pk=book.pk).aupdate(pages=book.pages + 1)
    assert [book.pages async for book in Book.objects.order_by("pages")] == [257, 413, 475]


@pytest.mark.usefixtures("library")
async def test_aiterator_runs_inside_an_open_sync_transaction() -> None:
    async def titles() -> list[str]:
        return [book.title async for book in Book.objects.order_by("title").aiterator(chunk_size=1)]

    def add_one_and_read() -> list[str]:
        with transaction.atomic():
            add_book("Persuasion", 249, Author.objects.get(name="Jane Austen"))
            found = async_to_sync(titles)()
            transaction.set_rollback(True)
        return found

    assert await sync_to_async(add_one_and_read)() == ["Dune", "Dune Messiah", "Emma", "Persuasion"]


@pytest.mark.usefixtures("library")
async def test_first_last_earliest_and_latest() -> None:
    assert (await Book.objects.order_by("pages").afirst()).title == "Dune Messiah"  # type: ignore[union-attr]
    assert (await Book.objects.order_by("pages").alast()).title == "Emma"  # type: ignore[union-attr]
    assert await Book.objects.filter(pages__gt=1000).afirst() is None
    assert (await Book.objects.aearliest("pages")).title == "Dune Messiah"
    assert (await Book.objects.alatest("pages")).title == "Emma"


@pytest.mark.usefixtures("library")
async def test_count_exists_and_aggregate() -> None:
    assert await Book.objects.acount() == 3
    assert await Book.objects.filter(pages__gt=400).acount() == 2
    assert await Book.objects.filter(title__startswith="Dune").aexists() is True
    assert await Book.objects.filter(title="Ulysses").aexists() is False
    assert await Book.objects.aaggregate(total=Sum("pages"), longest=Max("pages")) == {"total": 1142, "longest": 474}
    authors = Author.objects.annotate(books_written=Count("books")).filter(books_written__gt=1)
    assert await authors.acount() == 1


@pytest.mark.usefixtures("library")
async def test_acontains_checks_by_primary_key() -> None:
    emma = await Book.objects.aget(title="Emma")
    assert await Book.objects.filter(pages__gt=400).acontains(emma) is True
    assert await Book.objects.filter(pages__lt=400).acontains(emma) is False


@pytest.mark.usefixtures("library")
async def test_an_empty_filter_gives_empty_answers() -> None:
    # Django knows `id__in=[]` matches nothing and runs no SQL at all.
    nothing = Book.objects.filter(id__in=[])
    assert [book async for book in nothing] == []
    assert await nothing.acount() == 0
    assert await nothing.aexists() is False
    assert await nothing.aupdate(pages=1) == 0
    assert await nothing.adelete() == (0, {})


# ---------------------------------------------------------------- writing


async def test_acreate_saves_the_book_and_sets_its_id() -> None:
    author = await Author.objects.acreate(name="Toni Morrison")
    book = await Book.objects.acreate(
        title="Beloved", pages=324, price=decimal.Decimal("12.50"), published=PUBLISHED, author=author
    )
    assert book.pk is not None
    assert book.in_print is True, "the field's default"
    assert book._state.adding is False
    saved = await Book.objects.select_related("author").aget(pk=book.pk)
    assert (saved.title, saved.price, saved.author.name) == ("Beloved", decimal.Decimal("12.50"), "Toni Morrison")


async def test_acreate_sends_pre_save_and_post_save() -> None:
    received: list[tuple[str, Any]] = []

    def before_save(sender: type[Author], instance: Author, **kwargs: Any) -> None:
        received.append(("pre_save", instance.pk))

    async def after_save(sender: type[Author], instance: Author, created: bool, **kwargs: Any) -> None:
        received.append(("post_save", created))

    pre_save.connect(before_save, sender=Author)
    post_save.connect(after_save, sender=Author)
    try:
        await Author.objects.acreate(name="Ursula K. Le Guin")
    finally:
        pre_save.disconnect(before_save, sender=Author)
        post_save.disconnect(after_save, sender=Author)
    assert received == [("pre_save", None), ("post_save", True)]


async def test_a_failed_write_raises_djangos_integrity_error() -> None:
    with pytest.raises(IntegrityError, match=r"(?i)not.null"):
        await Author.objects.acreate(name=None)  # type: ignore[misc]  # a missing name, on purpose


@pytest.mark.usefixtures("library")
async def test_aupdate_changes_the_rows_and_counts_them() -> None:
    changed = await Book.objects.filter(author__name="Frank Herbert").aupdate(pages=F("pages") + 1)
    assert changed == 2
    assert [book.pages async for book in Book.objects.order_by("title")] == [413, 257, 474]


@pytest.mark.usefixtures("library")
async def test_adelete_deletes_the_rows_and_counts_them() -> None:
    assert await Book.objects.filter(title__startswith="Dune").adelete() == (2, {"library.Book": 2})
    assert await Book.objects.acount() == 1


async def test_adelete_of_nothing_lists_no_model() -> None:
    assert await Book.objects.filter(title="Ulysses").adelete() == (0, {}), "like Django's delete()"


@pytest.mark.usefixtures("library")
async def test_adelete_with_cascades_deletes_the_related_rows_too() -> None:
    # Deleting an author deletes their books (`on_delete=CASCADE`): Django
    # finds them first, so this runs Django's own `adelete()`.
    deleted = await Author.objects.filter(name="Frank Herbert").adelete()
    assert deleted == (3, {"library.Book": 2, "library.Author": 1})
    assert await Book.objects.acount() == 1


# ---------------------------------------------------------------- falling back to Django


@pytest.mark.usefixtures("library")
async def test_prefetch_related_still_works() -> None:
    authors = [author async for author in Author.objects.prefetch_related("books").order_by("name")]
    assert [len(author.books.all()) for author in authors] == [2, 1]


async def test_get_or_create_still_works() -> None:
    author, created = await Author.objects.aget_or_create(name="Octavia E. Butler")
    assert created is True
    again, created = await Author.objects.aget_or_create(name="Octavia E. Butler")
    assert (again.pk, created) == (author.pk, False)


# ---------------------------------------------------------------- no thread waits


@pytest.mark.usefixtures("library")
async def test_queries_dont_wait_for_djangos_sync_thread() -> None:
    # Django's own async methods all run on one shared "sync thread", one at
    # a time. Keep that thread busy: djox's queries must not wait for it.
    release = threading.Event()
    busy = asyncio.ensure_future(sync_to_async(release.wait, thread_sensitive=True)())
    try:
        book = await asyncio.wait_for(Book.objects.aget(title="Dune"), timeout=5)
        count = await asyncio.wait_for(Book.objects.acount(), timeout=5)
    finally:
        release.set()
        await busy
    assert (book.title, count) == ("Dune", 3)


@pytest.mark.usefixtures("library")
async def test_many_queries_at_once() -> None:
    titles = ["Dune", "Dune Messiah", "Emma"] * 20
    books = await asyncio.gather(*(Book.objects.aget(title=title) for title in titles))
    assert [book.title for book in books] == titles


# ---------------------------------------------------------------- inside a sync transaction


def test_async_queries_inside_atomic_see_its_rows() -> None:
    # The classic case is Django's `TestCase`: the test runs inside `atomic()`,
    # and its async code must see the rows the test wrote.
    async def count_and_add() -> int:
        await Author.objects.acreate(name="Inside")
        return await Author.objects.acount()

    with transaction.atomic():
        Author.objects.create(name="Not committed yet")
        assert async_to_sync(count_and_add)() == 2
        assert Author.objects.count() == 2, "the async write joined the transaction"
        transaction.set_rollback(True)
    assert Author.objects.count() == 0, "and was rolled back with it"


def test_unrelated_async_code_doesnt_join_the_transaction() -> None:
    counts: list[int] = []

    def count_in_another_thread() -> None:
        # A new thread starts with an empty context, like another request.
        counts.append(asyncio.run(Author.objects.acount()))

    with transaction.atomic():
        Author.objects.create(name="Not committed yet")
        thread = threading.Thread(target=count_in_another_thread)
        thread.start()
        thread.join()
    assert counts == [0], "it only sees committed rows"
    assert Author.objects.count() == 1


def test_async_queries_are_logged_like_djangos() -> None:
    async def read() -> None:
        await Book.objects.filter(pages__gt=300).acount()

    with transaction.atomic(), CaptureQueriesContext(connection) as captured:
        async_to_sync(read)()
    assert len(captured.captured_queries) == 1
    assert "COUNT(*)" in captured.captured_queries[0]["sql"]
    assert "300" in captured.captured_queries[0]["sql"]


def test_djox_manager_is_a_django_manager() -> None:
    assert isinstance(Book.objects.all(), djox.QuerySet)
    assert Book.objects.filter(pages__gt=1).count() == 0, "sync methods are Django's"
