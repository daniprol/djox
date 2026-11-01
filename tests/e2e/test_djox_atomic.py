"""`djox.atomic()` and `djox.on_commit()`: Django's transactions, in async code.

Each test runs without a test transaction (`transaction=True`), like a real
app: the block's `COMMIT` really commits, and other connections only see the
rows once it has.
"""

import asyncio
import contextvars
import threading

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import IntegrityError, OperationalError, transaction
from django.db import connections as django_connections

import djox
from tests.e2e.library.models import Author

pytestmark = pytest.mark.django_db(transaction=True)


class ChangedMyMindError(Exception):
    """Raised by the tests to leave a block with an error."""


async def names() -> list[str]:
    """The authors' names, read outside any transaction (so only committed rows)."""

    async def read() -> list[str]:
        return [name async for name in Author.objects.order_by("name").values_list("name", flat=True)]

    # A task with an empty context is like another request: it is in no transaction.
    return await asyncio.create_task(read(), context=contextvars.Context())


# ---------------------------------------------------------------- commit and rollback


async def test_the_block_commits_when_it_ends() -> None:
    async with djox.atomic():
        await Author.objects.acreate(name="Frank Herbert")
        await Author.objects.acreate(name="Jane Austen")
        assert await names() == [], "nothing is committed yet"
        assert await Author.objects.acount() == 2, "but the block sees its own rows"
    assert await names() == ["Frank Herbert", "Jane Austen"]


async def test_an_exception_rolls_the_block_back_and_goes_on() -> None:
    async def add_and_change_my_mind() -> None:
        async with djox.atomic():
            await Author.objects.acreate(name="Frank Herbert")
            raise ChangedMyMindError

    with pytest.raises(ChangedMyMindError):
        await add_and_change_my_mind()
    assert await names() == []


@djox.atomic
async def add_and_change_my_mind(name: str) -> None:
    """Add an author in a block, then leave the block with an error."""
    await Author.objects.acreate(name=name)
    raise ChangedMyMindError


async def test_a_nested_block_is_a_savepoint() -> None:
    async with djox.atomic():
        await Author.objects.acreate(name="Kept")
        with pytest.raises(ChangedMyMindError):
            await add_and_change_my_mind("Rolled back")
        await Author.objects.acreate(name="Kept too")
    assert await names() == ["Kept", "Kept too"]


async def test_a_failed_write_spoils_the_block_like_in_django() -> None:
    # Catching the error inside the block doesn't undo it: Django refuses
    # further queries, and the block rolls back.
    async def add_fail_and_go_on() -> None:
        async with djox.atomic():
            await Author.objects.acreate(name="Rolled back")
            with pytest.raises(IntegrityError):
                await Author.objects.acreate(name=None)  # type: ignore[misc]  # a missing name, on purpose
            await Author.objects.acount()

    with pytest.raises(transaction.TransactionManagementError):
        await add_fail_and_go_on()
    assert await names() == []


async def test_a_failed_write_in_a_nested_block_only_undoes_that_block() -> None:
    async with djox.atomic():
        await Author.objects.acreate(name="Kept")
        with pytest.raises(IntegrityError):
            async with djox.atomic():
                await Author.objects.acreate(name=None)  # type: ignore[misc]  # a missing name, on purpose
    assert await names() == ["Kept"]


async def test_durable_refuses_to_be_nested() -> None:
    async with djox.atomic():
        with pytest.raises(RuntimeError, match="durable"):
            async with djox.atomic(durable=True):
                pass


# ---------------------------------------------------------------- everything inside joins


async def test_djangos_own_async_methods_join_the_block() -> None:
    # `aget_or_create()` is Django's own method (djox falls back to it).
    async def get_or_create_and_change_my_mind() -> None:
        async with djox.atomic():
            author, created = await Author.objects.aget_or_create(name="Octavia E. Butler")
            assert created is True
            assert await Author.objects.filter(pk=author.pk).aexists(), "djox sees Django's write"
            raise ChangedMyMindError

    with pytest.raises(ChangedMyMindError):
        await get_or_create_and_change_my_mind()
    assert await names() == [], "and it was rolled back with the block"


async def test_sync_to_async_code_runs_in_the_block() -> None:
    def count_with_django() -> int:
        return Author.objects.count()

    async with djox.atomic():
        await Author.objects.acreate(name="Inside")
        assert await sync_to_async(count_with_django)() == 1


async def test_tasks_started_inside_share_the_block() -> None:
    async with djox.atomic():
        await asyncio.gather(*(Author.objects.acreate(name=f"Author {number}") for number in range(5)))
        assert await Author.objects.acount() == 5
    assert len(await names()) == 5


# ---------------------------------------------------------------- the decorator


async def test_atomic_decorates_async_functions() -> None:
    @djox.atomic
    async def add_two(first: str, second: str) -> None:
        await Author.objects.acreate(name=first)
        await Author.objects.acreate(name=second)

    @djox.atomic(using="default")
    async def add_and_fail(name: str) -> None:
        await Author.objects.acreate(name=name)
        raise ChangedMyMindError

    await add_two("Frank Herbert", "Jane Austen")
    with pytest.raises(ChangedMyMindError):
        await add_and_fail("Rolled back")
    assert await names() == ["Frank Herbert", "Jane Austen"]


# ---------------------------------------------------------------- on_commit


async def test_on_commit_runs_after_the_commit_only() -> None:
    committed: list[str] = []

    @djox.atomic
    async def plan_and_change_my_mind() -> None:
        await djox.on_commit(lambda: committed.append("rolled back"))
        raise ChangedMyMindError

    async with djox.atomic():
        await djox.on_commit(lambda: committed.append("first"))
        assert committed == [], "not before the commit"
    with pytest.raises(ChangedMyMindError):
        await plan_and_change_my_mind()
    await djox.on_commit(lambda: committed.append("no transaction: right away"))
    assert committed == ["first", "no transaction: right away"]


# ---------------------------------------------------------------- many at once


async def test_transactions_at_once_each_commit_their_own_rows() -> None:
    async def add(number: int) -> None:
        async with djox.atomic():
            await Author.objects.acreate(name=f"Author {number:02}")
            await asyncio.sleep(0)  # let the other transactions run
            await Author.objects.filter(name=f"Author {number:02}").aupdate(name=f"Author {number:02}!")

    await asyncio.gather(*(add(number) for number in range(20)))
    assert await names() == [f"Author {number:02}!" for number in range(20)]


async def test_a_block_gives_up_waiting_for_its_turn_after_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # SQLite has one writer, so blocks take turns. Like SQLite, a block waits
    # at most the `timeout` option for the block before it.
    monkeypatch.setitem(django_connections["default"].settings_dict["OPTIONS"], "timeout", 0.2)
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold_the_turn() -> None:
        async with djox.atomic():
            started.set()
            await release.wait()

    holder = asyncio.create_task(hold_the_turn())
    await started.wait()
    try:
        with pytest.raises(OperationalError, match="database is locked"):
            await add_and_change_my_mind("Never")
    finally:
        release.set()
        await holder


async def test_a_block_doesnt_wait_for_djangos_sync_thread() -> None:
    # Django's own async methods run on one shared "sync thread". Keep it
    # busy: a djox block has its own thread, so it must not wait.
    release = threading.Event()
    busy = asyncio.ensure_future(sync_to_async(release.wait, thread_sensitive=True)())
    try:
        async with asyncio.timeout(5), djox.atomic():
            await Author.objects.aget_or_create(name="Not waiting")
    finally:
        release.set()
        await busy
    assert await names() == ["Not waiting"]


# ---------------------------------------------------------------- inside a sync transaction


def test_inside_a_sync_transaction_the_block_is_a_savepoint() -> None:
    # Like Django's `TestCase`: the async code runs inside the test's `atomic()`.
    async def add_two_and_undo_one() -> None:
        async with djox.atomic():
            await Author.objects.acreate(name="Kept")
        with pytest.raises(ChangedMyMindError):
            await add_and_change_my_mind("Rolled back")

    with transaction.atomic():
        async_to_sync(add_two_and_undo_one)()
        assert list(Author.objects.values_list("name", flat=True)) == ["Kept"]
        transaction.set_rollback(True)
    assert Author.objects.count() == 0, "the outer sync block decides"


def test_the_block_cant_join_a_transaction_on_a_busy_thread() -> None:
    # `asyncio.run()` inside `atomic()` runs the event loop on the thread that
    # holds the transaction, so nothing else can run SQL in it.
    async def add() -> None:
        async with djox.atomic():
            await Author.objects.acreate(name="Never")

    with transaction.atomic(), pytest.raises(transaction.TransactionManagementError, match="another thread"):
        asyncio.run(add())
    assert Author.objects.count() == 0
