"""Async code in a sync transaction that Django's `TestCase` didn't open.

`TestCase` is the best-known case, but any `transaction.atomic()` block is one:
pytest-django's `django_db` tests (without `transaction=True`) run inside one,
and so does any code that calls `async_to_sync()` from inside a block. djox's
queries must join it, or they would not see the block's rows (and the block
would not see theirs).

Each test says in its name how the async code reaches the transaction.
"""

import asyncio
import contextvars
import threading
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.db import IntegrityError, connection, transaction

import djox
from tests.e2e.library.models import Author

pytestmark = pytest.mark.django_db


class Counter:
    """A hook that counts the statements djox runs."""

    def __init__(self) -> None:
        self.statements = 0

    def before_query(self, query: djox.Query) -> None:
        self.statements += 1


@pytest.fixture
def counted_statements() -> Any:
    """The `Counter` of the statements run while the test lasts."""
    counter = Counter()
    hooks = djox.Hooks(before_query=counter.before_query)
    djox.add_hooks(hooks)
    yield counter
    djox.remove_hooks(hooks)


# ---------------------------------------------------------------- an async test in pytest-django's transaction


async def test_an_async_test_sees_its_own_writes() -> None:
    # `django_db` without `transaction=True` runs the whole test in a sync
    # `atomic()` block, on the event loop's thread: the plain methods still join it.
    await Author.objects.acreate(name="Frank Herbert")
    assert await Author.objects.acount() == 1
    assert await Author.objects.aget(name="Frank Herbert")
    assert [name async for name in Author.objects.values_list("name", flat=True)] == ["Frank Herbert"]


async def test_tasks_running_together_share_the_one_connection() -> None:
    # The transaction has one connection, which runs one statement at a time:
    # concurrent tasks queue on it instead of failing.
    await asyncio.gather(*(Author.objects.acreate(name=f"Author {number}") for number in range(10)))
    counts = await asyncio.gather(*(Author.objects.acount() for _ in range(10)))
    assert counts == [10] * 10


async def test_a_task_with_its_own_context_is_outside_the_transaction() -> None:
    await Author.objects.acreate(name="Inside")
    # Another request starts with an empty context: it doesn't know about the transaction.
    # (It reads on djox's own connections, which can't see an uncommitted row.)
    outside = asyncio.create_task(Author.objects.acount(), context=contextvars.Context())
    assert await outside == 0
    assert await Author.objects.acount() == 1


# ---------------------------------------------------------------- async_to_sync inside a sync block


def test_a_sync_block_and_its_async_code_see_each_others_rows() -> None:
    async def write_and_read() -> int:
        await Author.objects.acreate(name="Written by async code")
        return await Author.objects.acount()

    Author.objects.create(name="Written by sync code")
    assert async_to_sync(write_and_read)() == 2
    assert Author.objects.count() == 2


def test_a_nested_block_rolls_back_what_async_code_wrote_in_it() -> None:
    async def add(name: str) -> None:
        await Author.objects.acreate(name=name)

    async_to_sync(add)("Kept")
    # `atomic()` inside the test's transaction is a savepoint.
    with pytest.raises(RuntimeError, match="changed my mind"), transaction.atomic():  # noqa: PT012
        async_to_sync(add)("Undone")
        raise RuntimeError("changed my mind")
    assert list(Author.objects.values_list("name", flat=True)) == ["Kept"]


def test_a_failed_async_write_breaks_the_transaction_like_a_sync_one() -> None:
    async def add_author_without_a_name() -> None:
        await Author.objects.acreate(name=None)  # type: ignore[misc]  # NOT NULL, on purpose

    with transaction.atomic():
        with pytest.raises(IntegrityError):
            async_to_sync(add_author_without_a_name)()
        # Like a sync error: the block is marked for rollback, so it can't commit half its work.
        assert connection.needs_rollback


def test_async_code_in_another_thread_is_outside_the_transaction() -> None:
    counts: list[int] = []

    def count_in_a_new_thread() -> None:
        counts.append(asyncio.run(Author.objects.acount()))

    Author.objects.create(name="Not committed")
    thread = threading.Thread(target=count_in_a_new_thread)
    thread.start()
    thread.join()
    assert counts == [0]


def test_statements_in_the_transaction_reach_the_hooks(counted_statements: Counter) -> None:
    async def count() -> int:
        return await Author.objects.acount()

    assert async_to_sync(count)() == 0
    assert counted_statements.statements == 1
