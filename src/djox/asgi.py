"""`djox.asgi`: Django's ASGI handler, without thread hops for its request signals.

```python
# asgi.py
from djox.asgi import get_asgi_application

application = get_asgi_application()
```

Django's ASGI handler sends `request_started` before each request and
`request_finished` after it (when it closes the response). Django's own
receivers of those signals are sync functions (`close_old_connections()`
closes old database connections, `reset_queries()` empties the query log),
so Django runs them with `sync_to_async()`: two hops to a worker thread per
request, and the first one starts a new thread for the request. On a small
machine that costs about 1 ms, more than the view itself.

This handler sends the same signals, with the same receivers, at the same
moments. It only runs them on the event loop's own thread when that can't
block it (`djox._request_signals` says when): every receiver is one of
Django's own (or djox's), and the request ran no sync code, whose thread
would hold the sync database connections `close_old_connections()` closes.
An async view that uses djox (or no database) is in that case. A sync
view, sync middleware or a receiver of your own makes the handler hop to a
thread, exactly like Django's.

Everything else is Django's `ASGIHandler`: reading the body, middleware,
views, sending the response, a client that disconnects early.
"""

import asyncio
from contextlib import closing
from typing import Any

import django
from django.core.exceptions import RequestAborted
from django.core.handlers import asgi
from django.urls import set_script_prefix

from djox import _request_signals


def get_asgi_application() -> "ASGIHandler":
    """djox's ASGI application, used like Django's `get_asgi_application()`."""
    django.setup(set_prefix=False)
    return ASGIHandler()


class ResponseSent(Exception):  # noqa: N818  (a signal between two lines of code, not an error)
    """Raised once the response is sent, to stop listening for a disconnect."""


class ASGIHandler(asgi.ASGIHandler):
    """Django's `ASGIHandler`, sending the request signals without thread hops
    when it can (see the module docs)."""

    async def handle(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Handle one HTTP request: Django's steps, in Django's order."""
        try:
            body_file = await self.read_body(receive)
        except RequestAborted:
            return  # the client left before sending its whole request

        # `closing()` closes the body file when the block ends, like a `with open(...)`.
        with closing(body_file):
            set_script_prefix(asgi.get_script_prefix(scope))
            await _request_signals.send_request_started(self.__class__, scope)
            request, error_response = self.create_request(scope, body_file)
            if request is None:
                assert error_response is not None, "create_request() returns a request or an error response"
                await self.send_response(error_response, send)
                await _request_signals.close_response(error_response)
                return
            response = await self.respond_unless_disconnected(request, receive, send)
            if response is None:
                await _request_signals.send_request_finished(self.__class__)
            else:
                await _request_signals.close_response(response)

    async def respond_unless_disconnected(self, request: Any, receive: Any, send: Any) -> Any:
        """Build and send the response, while listening for the client leaving.

        Returns the response, or `None` if the client left before it was
        built. Like Django's handler (6.1): if the client leaves, the view's
        task is cancelled, so the view can stop its work.
        """
        response = None
        try:
            # A `TaskGroup` runs its tasks together. When one raises, the
            # others are cancelled, and the group raises all their errors at
            # once (an `ExceptionGroup`, which `except*` takes apart).
            try:
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self.listen_for_disconnect(receive))
                    response = await self.run_get_response(request)
                    await self.send_response(response, send)
                    raise ResponseSent
            except* (ResponseSent, RequestAborted):
                pass
        except BaseExceptionGroup as errors:
            # Any other error is the view's: raise it as itself, like a sync view's would.
            if len(errors.exceptions) == 1:
                raise errors.exceptions[0]  # noqa: B904  (Django's handler raises it the same way)
            raise
        return response
