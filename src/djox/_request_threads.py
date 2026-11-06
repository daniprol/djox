"""One of djox's pooled threads for each request's sync code, under ASGI.

Under ASGI, Django runs a request's sync code (a sync view, sync middleware,
`sync_to_async()` in your own code) on a thread of the request's own, so all
of it shares the thread's database connections, like under WSGI. Django's
handler starts that thread the first time the request needs it, and at the
end of the request starts one more thread, only to wait for the first one to
stop. Starting threads costs more than a small view, and since every request
gets new connections, `CONN_MAX_AGE` can't keep any (Django's docs say to
turn it off under ASGI).

djox's handler (`djox.asgi`) gives each request a `RequestContext` instead.
It keeps Django's rule (one thread for all the sync code of a request) but
the thread is one of djox's pooled ones (`djox._threads`), taken the first
time the request runs sync code and given back when the request ends. The
thread keeps its connections between requests, and looks after them like a
WSGI thread does: Django's `close_old_connections()` runs on it when a
request starts using it, and again when the request finishes.

| Class            | What it is                                                      |
|------------------|-----------------------------------------------------------------|
| `RequestContext` | Around one request: sends its sync code to its `RequestThread`   |
| `RequestThread`  | The request's thread, taken from the pool on its first use       |
"""

from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from contextvars import Token
from types import TracebackType
from typing import Any

from asgiref.sync import SyncToAsync, ThreadSensitiveContext

from djox._threads import give_back_idle_thread, take_idle_thread, tidy_connections


class RequestThread(Executor):
    """The thread that runs one request's sync code.

    An `Executor` is what `asyncio` and asgiref hand functions to run
    (`submit()`); a `ThreadPoolExecutor` is one. This one has no thread of
    its own: it takes an idle pooled thread the first time it is given work,
    and hands every later call to that same thread.
    """

    def __init__(self) -> None:
        # `None` until the request runs sync code (`X | None` reads "an X, or None").
        self.thread: ThreadPoolExecutor | None = None

    @property
    def started(self) -> bool:
        """Whether the request ran (or is running) sync code."""
        return self.thread is not None

    # The `/` makes `function` positional only, like in `Executor.submit()`.
    def submit(self, function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        """Run `function(*args, **kwargs)` on the request's thread (asgiref calls this)."""
        if self.thread is None:
            self.thread = take_idle_thread()
            # The thread may hold connections from an earlier request: first
            # check them, as Django does when a request starts. The thread
            # runs one call at a time, in order, so this runs before `function`.
            self.thread.submit(tidy_connections)
        return self.thread.submit(function, *args, **kwargs)

    def give_back(self) -> None:
        """Give the thread back to the pool, once the request is over."""
        if self.thread is not None:
            give_back_idle_thread(self.thread)
            self.thread = None


class RequestContext(ThreadSensitiveContext):
    """Around one request: `async with RequestContext(): ...`.

    Like Django's `ThreadSensitiveContext()` around a request, but the
    request's sync code runs on a pooled `RequestThread`. asgiref finds the
    thread for a `sync_to_async()` call from the context the call runs in
    (a context variable, like a `threading.local()` that follows async
    tasks), in `SyncToAsync.context_to_thread_executor`: this class puts the
    request's thread there.

    A task the request started that outlives it finds no thread there any
    more, and asgiref starts one for it, as it does under Django's handler.
    """

    def __init__(self) -> None:
        super().__init__()
        self.request_thread = RequestThread()
        self.context_token: Token[ThreadSensitiveContext] | None = None

    async def __aenter__(self) -> "RequestContext":
        self.context_token = SyncToAsync.thread_sensitive_context.set(self)
        SyncToAsync.context_to_thread_executor[self] = self.request_thread
        return self

    # The parameter names are asgiref's: `exc` is the exception's class, `value` the exception.
    async def __aexit__(
        self,
        exc: type[BaseException] | None,
        value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        SyncToAsync.context_to_thread_executor.pop(self, None)
        if self.context_token is not None:
            SyncToAsync.thread_sensitive_context.reset(self.context_token)
            self.context_token = None
        self.request_thread.give_back()
