# Serve with ASGI

Django's ASGI handler starts a new thread for every request: for its
`request_started` and `request_finished` signals, and for the request's sync
code (a sync view, sync middleware). That costs about 1 ms on a small
machine, often more than the view. djox's handler sends the same signals
right on the event loop when that can't block it, and runs sync code on a
small pool of threads it keeps.

```python
# asgi.py
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mysite.settings")

from djox.asgi import get_asgi_application

application = get_asgi_application()  # was django.core.asgi.get_asgi_application()
```

Serve it as usual: `uvicorn mysite.asgi:application`, `granian`, `daphne`...

## What changes

Nothing you can see, but speed:

- **The same signals, receivers and moments.** `request_started` before the
  view, `request_finished` when the response is closed.
- **On the event loop when it is safe**: every receiver is one Django (or
  djox) adds, and the request ran no sync code. An async view that uses djox
  (or no database) is in that case.
- **On a thread, like Django, otherwise**: a sync view or sync middleware
  (its connections must be closed on its thread), a receiver of your own, a
  cache that closes connections (memcached, Redis), a file response.
- **One thread per request for sync code, like Django**, but a pooled one:
  taken when the request first runs sync code, given back when it ends.
- **`CONN_MAX_AGE` works under ASGI.** Django's docs say to turn persistent
  connections off under ASGI, since each request's new thread opens new
  connections. A pooled thread keeps its connections between requests, and
  checks them when a request starts and finishes, like under WSGI.

On the cloud VM djox is tested on, the same djox app and views, only the
handler changes (µs per request, one at a time, in the process;
[benchmarks](https://github.com/daniprol/djox/blob/main/benchmarks/README.md#asgi-handlers)):

| Request | Django's handler | djox's handler |
|---------|------------------|----------------|
| An async view reading one book | 1,524 µs | 266 µs |
| A sync view reading one book | 2,719 µs | 797 µs |

Sync views are still faster under WSGI: under ASGI, each one hops to a
thread for the view and again to close its connections.

## Good to know

- Everything else is Django's `ASGIHandler`: reading the body, middleware,
  views, sending the response, a client that disconnects early.
- It doesn't need `"djox"` in `INSTALLED_APPS`, nor djox's `ENGINE`, but
  only a view whose queries run on djox avoids the threads.
