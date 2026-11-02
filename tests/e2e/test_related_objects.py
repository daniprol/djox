"""`select_related()` and `prefetch_related()` on `djox.QuerySet`, sync and async.

`select_related()` joins the related tables into the query: the fast lane
runs it, and Rust builds the related objects too. `prefetch_related()` runs
the main query like any other (on the fast lane when it is simple), then
Django's own prefetching on one of djox's threads.

Each test checks that the objects are exactly Django's (computed with the
fast lane off, `djangos()`), and that no query runs later when the related
objects are used.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.core.exceptions import FieldError
from django.db import models
from django.db.models import Prefetch
from django.db.models.signals import post_init

import djox
from djox._fast_lane import statements
from djox._fast_lane.statements import Statement
from tests.e2e.library.models import Author, Biography, Book, Edition, Publisher
from tests.e2e.test_fast_lane import PUBLISHED, djangos

# A function that makes a fresh queryset. `Any`: django-stubs types the
# querysets as Django's, without djox's attributes.
MakeQuerySet = Callable[[], Any]

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def no_statements_yet() -> Iterator[None]:
    """Each test starts with the fast lane knowing no shape yet."""
    statements.statements.clear()
    yield
    statements.statements.clear()


@pytest.fixture
def library() -> None:
    """Two authors (one with a biography), three books, and four editions
    (one without a publisher), written with Django's sync ORM."""
    herbert = Author.objects.create(name="Frank Herbert")
    austen = Author.objects.create(name="Jane Austen")
    Biography.objects.create(author=herbert, text="Born in Tacoma.")
    ace = Publisher.objects.create(name="Ace")
    chilton = Publisher.objects.create(name="Chilton")
    for title, pages, author in [("Dune", 412, herbert), ("Dune Messiah", 256, herbert), ("Emma", 474, austen)]:
        Book.objects.create(title=title, pages=pages, price="9.99", published=PUBLISHED, author=author)
    dune = Book.objects.get(title="Dune")
    Edition.objects.create(book=dune, publisher=chilton, number=1)
    Edition.objects.create(book=dune, publisher=ace, number=2)
    Edition.objects.create(book=dune, publisher=None, number=3)
    Edition.objects.create(book=Book.objects.get(title="Emma"), publisher=None, number=1)
    statements.statements.clear()  # the tests count the statements of their own queries


@contextmanager
def recorded_queries() -> Iterator[list[str]]:
    """The SQL of every statement run inside the block, sync or async (a
    `djox.hooks` hook sees them all, on any thread)."""
    queries: list[str] = []

    def record(query: djox.Query) -> None:
        queries.append(query.sql)

    hooks = djox.Hooks(before_query=record)
    djox.add_hooks(hooks)
    try:
        yield queries
    finally:
        djox.remove_hooks(hooks)


def fast_statements() -> list[Statement]:
    """The statements the fast lane kept (shapes it runs itself)."""
    return [found for found in statements.statements.values() if isinstance(found, Statement)]


def fields_of(instance: models.Model | None, *relations: str) -> Any:
    """An instance's field values and state, with those of the related
    objects named in `relations` (like `"book__author"`), to compare two
    instances and everything cached on them."""
    if instance is None:
        return None
    values = {name: value for name, value in instance.__dict__.items() if name != "_state"}
    found = {**values, "db": instance._state.db, "adding": instance._state.adding}
    for relation in relations:
        name, _, rest = relation.partition("__")
        found[name] = fields_of(getattr(instance, name), *([rest] if rest else []))
    return found


# ---------------------------------------------------------------- select_related


# Querysets with `select_related()`, and the relations to compare.
SELECT_RELATED: dict[str, tuple[MakeQuerySet, tuple[str, ...]]] = {
    "a foreign key": (lambda: Book.objects.select_related("author").order_by("pk"), ("author",)),
    "two levels and a nullable foreign key": (
        lambda: Edition.objects.select_related("book__author", "publisher").order_by("pk"),
        ("book__author", "publisher"),
    ),
    "a one-to-one, backwards": (lambda: Author.objects.select_related("biography").order_by("pk"), ()),
    "filtered and sliced": (
        lambda: Edition.objects.filter(number__gt=1).select_related("publisher").order_by("-number")[:2],
        ("publisher",),
    ),
}


@pytest.mark.parametrize(("make_queryset", "relations"), SELECT_RELATED.values(), ids=SELECT_RELATED.keys())
@pytest.mark.usefixtures("library")
async def test_select_related_gives_djangos_objects(make_queryset: MakeQuerySet, relations: tuple[str, ...]) -> None:
    expected = await sync_to_async(djangos)(lambda: [fields_of(found, *relations) for found in make_queryset()])
    queryset = make_queryset()
    found = [instance async for instance in queryset]
    assert queryset._recipe is not None, "the fast lane ran it: Django's Query was never built"
    assert [fields_of(instance, *relations) for instance in found] == expected
    assert len(fast_statements()) == 1


@pytest.mark.usefixtures("library")
def test_select_related_takes_the_fast_lane_in_sync_code_too() -> None:
    expected = djangos(
        lambda: [fields_of(edition, "book__author") for edition in Edition.objects.select_related("book__author")]
    )
    assert [
        fields_of(edition, "book__author") for edition in Edition.objects.select_related("book__author")
    ] == expected
    assert len(fast_statements()) == 1


@pytest.mark.usefixtures("library")
async def test_related_objects_need_no_more_queries() -> None:
    editions = [edition async for edition in Edition.objects.select_related("book__author", "publisher").order_by("pk")]
    with recorded_queries() as queries:
        names = [(edition.book.author.name, edition.publisher and edition.publisher.name) for edition in editions]
    assert queries == []
    assert names == [
        ("Frank Herbert", "Chilton"),
        ("Frank Herbert", "Ace"),
        ("Frank Herbert", None),
        ("Jane Austen", None),
    ]


@pytest.mark.usefixtures("library")
async def test_a_one_to_one_is_cached_both_ways() -> None:
    herbert, austen = [author async for author in Author.objects.select_related("biography").order_by("pk")]
    with recorded_queries() as queries:
        assert herbert.biography.author is herbert, "Django's setter caches the way back too"
        with pytest.raises(Biography.DoesNotExist):
            austen.biography  # noqa: B018  (reading it is the test)
    assert queries == []


@pytest.mark.usefixtures("library")
async def test_aget_and_acount_take_the_fast_lane_with_select_related() -> None:
    dune = await Book.objects.select_related("author").aget(title="Dune")
    assert dune.author.name == "Frank Herbert"
    assert await Book.objects.select_related("author").filter(pages__gt=300).acount() == 2
    assert len(fast_statements()) == 2


@pytest.mark.usefixtures("library")
async def test_select_related_none_forgets_the_relations() -> None:
    books = [book async for book in Book.objects.select_related("author").select_related(None)]
    author_field: Any = Book._meta.get_field("author")  # `Any`: django-stubs types it as any `Field`
    assert all(not author_field.is_cached(book) for book in books)


@pytest.mark.usefixtures("library")
async def test_values_after_select_related_gives_dicts() -> None:
    austen = await Author.objects.aget(name="Jane Austen")
    queryset = Book.objects.select_related("author").filter(title="Emma").values("title", "author")
    assert [row async for row in queryset] == [{"title": "Emma", "author": austen.pk}]


def test_select_related_after_values_raises_djangos_error() -> None:
    with pytest.raises(TypeError, match=r"Cannot call select_related\(\) after .values\(\)"):
        Book.objects.values("title").select_related("author")


@pytest.mark.usefixtures("library")
async def test_a_wrong_name_raises_when_the_query_runs_like_in_django() -> None:
    queryset = Book.objects.select_related("no_such_relation")  # type: ignore[misc]  # a wrong name, on purpose: no error yet, like Django
    with pytest.raises(FieldError, match="Invalid field name"):
        [book async for book in queryset]


@pytest.mark.usefixtures("library")
async def test_a_related_model_with_init_receivers_is_built_by_django() -> None:
    await Book.objects.select_related("author").acount()
    [book async for book in Book.objects.select_related("author")]  # the statement is kept
    initialized: list[Author] = []

    def receiver(sender: type[Author], instance: Author, **kwargs: Any) -> None:
        initialized.append(instance)

    post_init.connect(receiver, sender=Author)
    try:
        books = [book async for book in Book.objects.select_related("author")]
    finally:
        post_init.disconnect(receiver, sender=Author)
    assert [book.author for book in books] == initialized, "Django built the authors, and sent post_init"


# ---------------------------------------------------------------- prefetch_related


def titles_by_author(authors: list[Author]) -> list[tuple[str, list[str]]]:
    """Each author's name with the titles of the books prefetched for it."""
    return [(author.name, sorted(book.title for book in author.books.all())) for author in authors]


@pytest.mark.usefixtures("library")
async def test_prefetch_related_gives_djangos_objects() -> None:
    expected = await sync_to_async(djangos)(lambda: titles_by_author(list(Author.objects.prefetch_related("books"))))
    queryset: Any = Author.objects.prefetch_related("books").order_by("pk")
    with recorded_queries() as queries:
        authors = [author async for author in queryset]
        assert len(queries) == 2, "the authors, then all their books at once"
        assert titles_by_author(authors) == expected
        assert len(queries) == 2, "using the books runs no more queries"
    assert queryset._recipe is not None, "the authors' query ran on the fast lane"
    assert len(fast_statements()) == 1


@pytest.mark.usefixtures("library")
def test_prefetch_related_takes_the_fast_lane_in_sync_code_too() -> None:
    authors = list(Author.objects.prefetch_related("books").order_by("pk"))
    assert titles_by_author(authors) == [("Frank Herbert", ["Dune", "Dune Messiah"]), ("Jane Austen", ["Emma"])]
    assert len(fast_statements()) == 1


@pytest.mark.usefixtures("library")
async def test_prefetch_related_runs_off_the_event_loop() -> None:
    threads: list[str] = []

    def note_thread(query: djox.Query) -> None:
        threads.append(threading.current_thread().name)

    hooks = djox.Hooks(before_query=note_thread)
    djox.add_hooks(hooks)
    try:
        [author async for author in Author.objects.prefetch_related("books")]
    finally:
        djox.remove_hooks(hooks)
    _authors_query, books_query = threads
    assert books_query.startswith("djox"), "the prefetching ran on one of djox's threads"


@pytest.mark.usefixtures("library")
async def test_prefetch_objects_nested_lookups_and_to_attr() -> None:
    def make_queryset() -> Any:
        long_books = Book.objects.filter(pages__gt=300).order_by("title")
        return Author.objects.order_by("pk").prefetch_related(
            Prefetch("books", queryset=long_books, to_attr="long_books"),
            "books__editions__publisher",
        )

    def describe(authors: list[Author]) -> list[Any]:
        return [
            (
                [book.title for book in author.long_books],  # type: ignore[attr-defined]  # added by to_attr
                [
                    [edition.publisher and edition.publisher.name for edition in book.editions.all()]
                    for book in author.books.all()
                ],
            )
            for author in authors
        ]

    expected = await sync_to_async(djangos)(lambda: describe(list(make_queryset())))
    authors = [author async for author in make_queryset()]
    with recorded_queries() as queries:
        assert describe(authors) == expected
    assert queries == []


@pytest.mark.usefixtures("library")
async def test_prefetch_related_sees_the_writes_of_its_transaction() -> None:
    async with djox.atomic():
        austen = await Author.objects.aget(name="Jane Austen")
        await Book.objects.acreate(title="Persuasion", pages=249, price="9.99", published=PUBLISHED, author=austen)
        authors = [author async for author in Author.objects.prefetch_related("books").order_by("pk")]
    assert titles_by_author(authors)[1] == ("Jane Austen", ["Emma", "Persuasion"])


@pytest.mark.usefixtures("library")
async def test_aiterator_prefetches_each_chunk() -> None:
    with recorded_queries() as queries:
        authors = [
            author async for author in Author.objects.prefetch_related("books").order_by("pk").aiterator(chunk_size=1)
        ]
    assert titles_by_author(authors) == [("Frank Herbert", ["Dune", "Dune Messiah"]), ("Jane Austen", ["Emma"])]
    # The authors' query (streamed, so logged once), and the books of each chunk.
    assert len(queries) == 3


@pytest.mark.usefixtures("library")
async def test_aprefetch_related_objects_prefetches_on_a_list() -> None:
    books = [book async for book in Book.objects.order_by("pk")]
    await djox.aprefetch_related_objects(books, "editions")
    with recorded_queries() as queries:
        assert [len(book.editions.all()) for book in books] == [3, 0, 1]
    assert queries == []


async def test_aprefetch_related_objects_with_no_objects_does_nothing() -> None:
    await djox.aprefetch_related_objects([], "editions")
