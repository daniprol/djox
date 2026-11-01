"""Write turns: plain async writes and transactions take turns in asyncio.

Plain writes (`acreate()` outside a transaction) share the turn: djox's
writer thread runs them one after the other. A transaction (`djox.atomic()`,
or Django's write methods on djox's threads) has it alone. So writers never
wait inside SQLite, whose retries sleep up to 100 ms.
"""

import asyncio
import time
from collections.abc import Awaitable

import pytest
from django.db import OperationalError
from django.db import connections as django_connections

import djox
from tests.e2e.library.models import Author

pytestmark = pytest.mark.django_db(transaction=True)


async def hold_a_transaction(started: asyncio.Event, release: asyncio.Event) -> None:
    """Open a block with one write, and keep it open until `release` is set."""
    async with djox.atomic():
        await Author.objects.acreate(name="In the block")
        started.set()
        await release.wait()


async def test_a_plain_write_waits_for_the_transaction_then_runs_at_once() -> None:
    started, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(hold_a_transaction(started, release))
    await started.wait()

    plain_write = asyncio.create_task(Author.objects.acreate(name="Plain"))
    done, _ = await asyncio.wait([plain_write], timeout=0.5)
    assert not done, "the plain write waits while the block has the turn"

    release.set()
    await holder
    # Waiting inside SQLite, it would sleep up to 100 ms between retries by now.
    committed = time.perf_counter()
    await plain_write
    assert time.perf_counter() - committed < 0.03, "it runs right after the commit"
    names = [name async for name in Author.objects.order_by("id").values_list("name", flat=True)]
    assert names == ["In the block", "Plain"]


async def test_a_plain_write_gives_up_after_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(django_connections["default"].settings_dict["OPTIONS"], "timeout", 0.2)
    started, release = asyncio.Event(), asyncio.Event()
    holder = asyncio.create_task(hold_a_transaction(started, release))
    await started.wait()
    try:
        with pytest.raises(OperationalError, match="database is locked"):
            await Author.objects.acreate(name="Too late")
    finally:
        release.set()
        await holder


async def test_plain_writes_and_transactions_at_once_all_land() -> None:
    async def plain(number: int) -> None:
        await Author.objects.acreate(name=f"plain {number:02}")

    @djox.atomic
    async def in_a_block(number: int) -> None:
        await Author.objects.acreate(name=f"block {number:02}")
        await Author.objects.aget_or_create(name=f"made in block {number:02}")

    tasks: list[Awaitable[object]] = []
    for number in range(20):
        tasks.append(plain(number))
        tasks.append(in_a_block(number))
        tasks.append(Author.objects.aget_or_create(name=f"get or create {number:02}"))
    async with asyncio.timeout(10):
        await asyncio.gather(*tasks)
    assert await Author.objects.acount() == 80
