"""Completing an asyncio Future from a database thread.

When a statement finishes, the Rust worker thread calls
`loop.call_soon_threadsafe(set_result, future, value)`. The event loop then runs
these functions on its own thread, the only place a Future may be changed.
"""

import asyncio
from typing import Any


def set_result(future: asyncio.Future[Any], result: Any) -> None:
    """Give `future` its result, unless it was cancelled meanwhile."""
    if not future.done():
        future.set_result(result)


def set_exception(future: asyncio.Future[Any], exception: BaseException) -> None:
    """Make `future` raise `exception`, unless it was cancelled meanwhile."""
    if not future.done():
        future.set_exception(exception)
