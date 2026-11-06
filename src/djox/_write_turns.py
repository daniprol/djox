"""Write turns: who may write to a SQLite file, and when, without SQLite's slow waiting.

SQLite lets one connection write at a time. djox writes through two kinds of
connection:

- **Plain writes** (`acreate()`, `aupdate()`, a fast `adelete()` outside a
  transaction) run on djox's writer connection: right away if they are
  quick, otherwise on its writer thread, one after the other. Many can be
  on their way at once: they never fight each other.
- **Transactions**: a `djox.atomic()` block runs on the writer connection
  too (`djox._writer_transactions`), and holds it until it ends. Django's
  own write methods outside a block (`aget_or_create()`, `abulk_create()`...
  on djox's threads, see `djox._threads`) run on a Django connection of
  their own. djox's `ENGINE` starts transactions with `BEGIN IMMEDIATE`,
  which takes SQLite's write lock at once.

If both kinds write at the same time, one of them finds the file locked.
SQLite then sleeps and retries, with sleeps that grow up to 100 ms, so a
5 ms transaction could hold up every plain write for far longer. djox's
writer thread would sleep too, and the whole queue behind it with it.

So writers take turns here, in asyncio, before they reach SQLite: plain
writes share the turn (any number at once), a transaction holds it alone. A
transaction that wants the turn waits for the plain writes on their way to
finish; new plain writes still go first for a couple of milliseconds, then
wait behind it, so neither side can starve the other. Waiting is a `Future`
on the event loop: no thread sleeps, and the next writer starts the moment
the turn is free.

Like SQLite, a writer gives up after the alias's `timeout` option, with
"database is locked".

This covers the writes of one event loop. Writes from other threads (sync
Django code) or other processes still meet SQLite's own waiting.
"""

import asyncio
import threading
import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from django.db import OperationalError
from django.db import connections as django_connections

from djox.backends.sqlite3.base import DatabaseWrapper

# How long plain writes may still start once a transaction waits for the
# turn (see `WriteTurn.plain_write_may_start()`). A plain write takes a
# fraction of a millisecond, so a few of them fit.
PLAIN_WRITES_HEAD_START = 0.002  # seconds


class WriteTurn:
    """The write turn of one SQLite file (one alias), for one event loop.

    Like a readers-writer lock, where the "readers" are plain writes (they
    share) and the "writer" is a transaction (it is alone).
    """

    def __init__(self, alias: str, timeout: float) -> None:
        self.alias = alias
        self.timeout = timeout
        self.plain_writes = 0
        """Plain writes on their way to djox's writer thread."""
        self.transaction_has_it = False
        self.transactions_waiting = 0
        self.transaction_waiting_since: float | None = None
        """When the transaction first in line started waiting (event loop time), or `None`."""
        # The tasks waiting for the turn to change. Each waits on its own
        # `Future`, like a one-time `threading.Event`.
        self.waiting: list[asyncio.Future[None]] = []

    def plain_write_may_start(self) -> bool:
        """Whether a new plain write may start now.

        Not while a transaction has the turn. While one waits for it, plain
        writes still go first for a moment (`PLAIN_WRITES_HEAD_START`): they
        are quick, and djox's writer thread runs them back to back. After
        that, new ones wait, so a steady flow of plain writes can't keep a
        transaction waiting forever.
        """
        if self.transaction_has_it:
            return False
        if self.transaction_waiting_since is None:
            return True
        waited = asyncio.get_running_loop().time() - self.transaction_waiting_since
        return waited < PLAIN_WRITES_HEAD_START

    async def start_plain_write(self) -> None:
        """Wait until a plain write may start, then join the plain writes."""
        if not self.plain_write_may_start():
            await self.wait_until(self.plain_write_may_start)
        self.plain_writes += 1

    def end_plain_write(self) -> None:
        """Leave the plain writes. The last one lets a waiting transaction in."""
        self.plain_writes -= 1
        if self.plain_writes == 0:
            self.wake_everyone()

    async def start_transaction(self) -> None:
        """Wait until nothing else writes, then take the turn alone."""
        self.transactions_waiting += 1
        if self.transaction_waiting_since is None:
            self.transaction_waiting_since = asyncio.get_running_loop().time()
        try:
            await self.wait_until(lambda: not self.transaction_has_it and self.plain_writes == 0)
        finally:
            self.transactions_waiting -= 1
            if self.transactions_waiting == 0:
                self.transaction_waiting_since = None
                # Plain writes may have been waiting behind this transaction (or for nothing, if it gave up).
                self.wake_everyone()
            else:
                # The next transaction in line starts its own wait now.
                self.transaction_waiting_since = asyncio.get_running_loop().time()
        self.transaction_has_it = True

    def end_transaction(self) -> None:
        """Give the turn back."""
        self.transaction_has_it = False
        self.wake_everyone()

    async def wait_until(self, can_go: Callable[[], bool]) -> None:
        """Wait until `can_go()` is true, for at most the `timeout` option."""
        try:
            async with asyncio.timeout(self.timeout):
                while not can_go():
                    woken: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                    self.waiting.append(woken)
                    await woken
        except TimeoutError:
            raise OperationalError(
                f"database is locked: another write to {self.alias!r} lasted more than "
                f"{self.timeout} seconds (the 'timeout' option)"
            ) from None

    def wake_everyone(self) -> None:
        """Wake every waiting task. Each checks again whether it can go now."""
        for woken in self.waiting:
            if not woken.done():  # a task that gave up (timed out) has cancelled its own
                woken.set_result(None)
        self.waiting.clear()


@asynccontextmanager
async def plain_write(alias: str) -> AsyncIterator[None]:
    """`async with plain_write(alias):` around a write sent to djox's writer thread."""
    turn = write_turn(alias, for_transactions=False)
    if turn is None:
        yield
        return
    await turn.start_plain_write()
    try:
        yield
    finally:
        turn.end_plain_write()


@asynccontextmanager
async def transaction_alone(alias: str) -> AsyncIterator[None]:
    """`async with transaction_alone(alias):` around a transaction on a Django connection."""
    turn = write_turn(alias, for_transactions=True)
    if turn is None:
        yield
        return
    await turn.start_transaction()
    try:
        yield
    finally:
        turn.end_transaction()


def write_turn(alias: str, for_transactions: bool) -> WriteTurn | None:
    """The write turn of `alias` for the running event loop, made on first use,
    or `None` when writers on `alias` needn't take turns:

    - its `ENGINE` isn't djox's (the database itself decides who waits);
    - for a transaction, its `transaction_mode` is deferred: the transaction
      takes SQLite's write lock only when it first writes, so it may well
      never compete. (Plain writes always take part: they must still wait
      for an immediate transaction.)
    """
    wrapper = django_connections[alias]
    if not isinstance(wrapper, DatabaseWrapper):
        return None
    options = wrapper.options()
    if for_transactions and options.transaction_mode not in ("IMMEDIATE", "EXCLUSIVE"):
        return None
    # An asyncio object belongs to one event loop, and a program may run
    # several loops (one per thread), so each loop has its own turns. A
    # `WeakKeyDictionary` forgets a loop's turns once the loop is gone.
    loop = asyncio.get_running_loop()
    with turns_lock:
        turns_of_this_loop = turns.setdefault(loop, {})
        if alias not in turns_of_this_loop:
            turns_of_this_loop[alias] = WriteTurn(alias, options.timeout)
        turn = turns_of_this_loop[alias]
    turn.timeout = options.timeout  # the settings may have changed (tests change them)
    return turn


turns: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, WriteTurn]]" = weakref.WeakKeyDictionary()
turns_lock = threading.Lock()
