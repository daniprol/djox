"""Hooks: `before_query` and `after_query` around every statement, sync or async."""

import decimal
from collections.abc import Iterator
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.db import IntegrityError
from django.db.models import Q

import djox
from tests.e2e.library.models import Author, Book

pytestmark = pytest.mark.django_db(transaction=True)


class Recorder:
    """Hooks as an object with methods: notes every statement and how it went."""

    def __init__(self) -> None:
        self.before: list[djox.Query] = []
        self.after: list[tuple[djox.Query, djox.QueryFinished]] = []

    def before_query(self, query: djox.Query) -> None:
        self.before.append(query)

    def after_query(self, query: djox.Query, finished: djox.QueryFinished) -> None:
        self.after.append((query, finished))

    def sql(self) -> list[str]:
        return [query.sql for query in self.before]


@pytest.fixture
def recorder() -> Iterator[Recorder]:
    recorder = Recorder()
    djox.add_hooks(recorder)
    yield recorder
    djox.remove_hooks(recorder)


@pytest.fixture
def herbert() -> Author:
    return Author.objects.create(name="Frank Herbert")


def new_book(author: Author, title: str = "Dune") -> Book:
    return Book(title=title, pages=412, price=decimal.Decimal("9.99"), published="2020-01-01T00:00Z", author=author)


def test_hooks_see_djangos_sync_orm(herbert: Author, recorder: Recorder) -> None:
    Book.objects.filter(Q(pages__gt=1) | Q(pages__lt=0)).count()  # Django's own way (a `Q()`)
    Book.objects.filter(pages__gt=1).count()  # the fast lane
    [first, second] = recorder.sql()[-2:]
    assert first.startswith("SELECT COUNT(*)")
    assert second.startswith("SELECT COUNT(*)")
    query, finished = recorder.after[-1]
    assert query.alias == "default"
    assert finished.error is None
    assert finished.seconds >= 0


async def test_hooks_see_async_queries_and_writes(herbert: Author, recorder: Recorder) -> None:
    await Book.objects.filter(pages__gt=1).acount()
    await Book.objects.filter(Q(pages__gt=1)).acount()
    await Book.objects.filter(pages__gt=1).aupdate(pages=2)
    book = new_book(herbert)
    await sync_to_async(book.save)()
    await Book.objects.filter(pk=book.pk).adelete()
    statements = [sql.split()[0] for sql in recorder.sql()]
    assert statements[:3] == ["SELECT", "SELECT", "UPDATE"]
    assert "INSERT" in statements
    assert statements[-1] == "DELETE"
    assert len(recorder.before) == len(recorder.after), "every statement finished once"


@pytest.mark.parametrize("use_async", [False, True])
async def test_before_query_can_change_what_runs(herbert: Author, use_async: bool) -> None:
    def ask_for_someone_else(query: djox.Query) -> djox.Query:
        if "library_author" in query.sql and query.parameters:
            return query.with_sql(f"/* changed */ {query.sql}").with_parameters(["Jane Austen"])
        return query

    hooks = djox.Hooks(before_query=ask_for_someone_else)
    djox.add_hooks(hooks)
    try:
        queryset = Author.objects.filter(name="Frank Herbert")
        count = await queryset.acount() if use_async else await sync_to_async(queryset.count)()
    finally:
        djox.remove_hooks(hooks)
    assert count == 0, "the changed parameters ran"


async def test_before_query_can_block_a_statement(herbert: Author) -> None:
    def read_only(query: djox.Query) -> None:
        if not query.sql.startswith("SELECT"):
            raise PermissionError("this database is read-only")

    hooks = djox.Hooks(before_query=read_only)
    djox.add_hooks(hooks)
    try:
        with pytest.raises(PermissionError, match="read-only"):
            await Author.objects.filter(name="Frank Herbert").aupdate(name="F. H.")
        assert await Author.objects.filter(name="Frank Herbert").acount() == 1
    finally:
        djox.remove_hooks(hooks)


def test_after_query_hears_about_errors(herbert: Author, recorder: Recorder) -> None:
    book = new_book(herbert)
    book.author_id = 999_999  # no such author: the foreign key fails
    with pytest.raises(IntegrityError):
        book.save()
    _query, finished = recorder.after[-1]
    assert isinstance(finished.error, IntegrityError)


def test_hooks_run_in_the_order_they_were_added(herbert: Author) -> None:
    seen: list[str] = []

    def first(query: djox.Query) -> djox.Query:
        seen.append("first")
        return query.with_sql(query.sql + " -- first")

    def second(query: djox.Query) -> None:
        seen.append("second saw " + query.sql.rsplit("--", 1)[-1].strip())

    hooks = [djox.Hooks(before_query=first), djox.Hooks(before_query=second)]
    for added in hooks:
        djox.add_hooks(added)
    try:
        Author.objects.filter(name="Frank Herbert").exists()
    finally:
        for added in hooks:
            djox.remove_hooks(added)
    assert seen == ["first", "second saw first"]


def test_removed_hooks_are_not_called(herbert: Author) -> None:
    calls: list[Any] = []
    hooks = djox.Hooks(after_query=lambda query, finished: calls.append(query))
    djox.add_hooks(hooks)
    djox.remove_hooks(hooks)
    Author.objects.count()
    assert calls == []
