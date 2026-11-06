"""Sending Django's request signals from async code, on the event loop when it is safe.

Django's ASGI handler sends `request_started` and `request_finished` with
`asend()`, which runs every sync receiver with `sync_to_async()`: a hop to a
worker thread and back. The usual receivers are small, and most can't block:

| Receiver                  | Signal             | What it does                              | Blocks?             |
|---------------------------|--------------------|-------------------------------------------|---------------------|
| `reset_queries()`         | `request_started`  | empties each connection's query log      | never               |
| `close_old_connections()` | both               | closes sync connections too old or broken | if one is open      |
| `close_caches()`          | `request_finished` | closes each cache's connections           | if a cache has them |
| `reset_urlconf()`         | `request_finished` | forgets a URLconf a middleware set        | never               |
| `note_request()`          | both               | djox's: counts requests (`djox._threads`) | never               |

Django keeps sync database connections per thread. Under ASGI, the sync
code of a request (a sync view, sync middleware) runs on a thread of the
request's own, which `sync_to_async()` starts the first time the request
needs it; `close_old_connections()` must run there, where the request's
connections are. An async view on djox never needs that thread: its queries
run on djox's own connections. So for such a request, every receiver above
can run right here, on the event loop's thread, doing the same work without
the hop. When the request did run sync code, or any receiver could block
(or is one of the program's own, which may do anything), the signal is sent
the way Django sends it.
"""

from collections.abc import Callable
from typing import Any

from asgiref.sync import SyncToAsync, sync_to_async
from django.core.cache import caches, close_caches
from django.core.cache.backends.base import BaseCache
from django.core.handlers.base import reset_urlconf
from django.core.signals import request_finished, request_started
from django.db import close_old_connections, reset_queries
from django.dispatch import Signal
from django.http import HttpRequest, HttpResponseBase

from djox._request_threads import RequestThread
from djox._threads import note_request


async def send_request_started(sender: type, scope: dict[str, Any]) -> None:
    """Send `request_started`, like Django's ASGI handler does."""
    await send(request_started, sender, scope=scope)


async def send_request_finished(sender: type) -> None:
    """Send `request_finished`, for a request that ended without a response
    (the client left): Django's ASGI handler sends it itself then."""
    await send(request_finished, sender)


async def close_response(response: HttpResponseBase) -> None:
    """Close a sent response, which also sends `request_finished`.

    Closing a response first closes what it holds: the request (and its
    uploaded files), and for `FileResponse` or `StreamingHttpResponse` an
    open file or iterator. Those may block, so such a response goes to a
    thread, like in Django.
    """
    # `_handler_class` is Django's: the handler that made the response, the signal's sender.
    sender = response._handler_class  # type: ignore[attr-defined]
    if closes_without_blocking(response) and can_run_here(request_finished, sender):
        response.close()
    else:
        await sync_to_async(response.close)()


def closes_without_blocking(response: HttpResponseBase) -> bool:
    """Whether everything `response.close()` closes is a request without
    uploaded files (Django adds the request to every response it handles)."""
    # `_resource_closers` is Django's list of the `close()` methods to call.
    for close in response._resource_closers:  # type: ignore[attr-defined]
        # `close.__self__` is the object a method belongs to.
        owner = getattr(close, "__self__", None)
        if not isinstance(owner, HttpRequest):
            return False  # a file, an iterator...
        if getattr(owner, "_files", None):
            return False  # uploaded files, maybe in temporary files on disk
    return True


async def send(signal: Signal, sender: type, **named: Any) -> None:
    """Send `signal` from async code: right here if that can't block, else with Django's `asend()`."""
    if can_run_here(signal, sender):
        signal.send(sender=sender, **named)
    else:
        await signal.asend(sender=sender, **named)


def can_run_here(signal: Signal, sender: type | None) -> bool:
    """Whether every receiver of `signal` can run now on the event loop's
    thread without blocking it (see the module docs)."""
    # `_live_receivers()` is Django's: the receivers listening now, sync ones and async ones.
    sync_receivers, async_receivers = signal._live_receivers(sender)
    if async_receivers:
        return False  # `asend()` awaits those; `send()` would start an event loop for them
    for receiver in sync_receivers:
        cannot_block = RECEIVERS_THAT_CANNOT_BLOCK_NOW.get(receiver)
        if cannot_block is None or not cannot_block():
            return False
    return True


def always() -> bool:
    return True


def ran_no_sync_code() -> bool:
    """Whether this request never ran sync code on a thread of its own.

    Django's handler gives each request a `ThreadSensitiveContext`: the first
    `sync_to_async()` call of the request starts a thread for it, and every
    later one runs there. djox's handler gives it a `RequestContext`, whose
    thread is taken from djox's pool on its first use. That thread holds the
    request's sync database connections, which `close_old_connections()`
    must close on it.
    """
    # asgiref's: the request's context, and the threads given to contexts.
    request_context = SyncToAsync.thread_sensitive_context.get(None)
    if request_context is None:
        return False  # outside a request's context: sync code went to a shared thread
    request_thread = SyncToAsync.context_to_thread_executor.get(request_context)
    if request_thread is None:
        return True  # Django's handler: no thread started yet
    if isinstance(request_thread, RequestThread):
        return not request_thread.started  # djox's handler: the pooled thread not taken yet
    return False


def no_cache_to_close() -> bool:
    """Whether every cache this request used closes without I/O: Django's
    in-memory, file and database caches do nothing in `close()`; memcached
    and Redis close their connections."""
    if not ran_no_sync_code():
        return False  # caches the sync code used live on its thread
    return all(type(cache).close is BaseCache.close for cache in caches.all(initialized_only=True))


# The receivers djox knows, and how to tell that one can't block right now.
RECEIVERS_THAT_CANNOT_BLOCK_NOW: dict[Callable[..., Any], Callable[[], bool]] = {
    reset_queries: always,
    reset_urlconf: always,
    note_request: always,
    close_old_connections: ran_no_sync_code,
    close_caches: no_cache_to_close,
}
