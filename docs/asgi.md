# Serve with ASGI

Django's ASGI handler hops to a worker thread twice per request, for its
`request_started` and `request_finished` signals: about 1 ms on a small
machine, often more than the view. djox's handler sends the same signals
right on the event loop when that can't block it.

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

An async view on djox, on the cloud VM djox is tested on:

| Request | Django's handler | djox's handler |
|---------|------------------|----------------|
| An empty async view | 1,300 µs | 160 µs |
| An async view reading one book | 1,700 µs | 470 µs |

## Good to know

- Everything else is Django's `ASGIHandler`: reading the body, middleware,
  views, sending the response, a client that disconnects early.
- It doesn't need `"djox"` in `INSTALLED_APPS`, nor djox's `ENGINE`, but
  only a view whose queries run on djox avoids the threads.
