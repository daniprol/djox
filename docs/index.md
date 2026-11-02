# Get started { .djox-visually-hidden }

<div class="djox-hero" markdown>

![djox](assets/logo-wordmark-light.svg#only-light)
![djox](assets/logo-wordmark-dark.svg#only-dark)

<p class="djox-tagline">Truly async Django ORM, with a Rust core.</p>
<p class="djox-subtitle">Keep your models, migrations and admin. Make <code>await Book.objects.aget(pk=1)</code> really async, and fast.</p>

[Install](#install){ .md-button .md-button--primary }
[How it works](how-it-works.md){ .md-button }

</div>

Django's async ORM isn't async underneath: `aget()`, `acount()` and
`async for` run the sync ORM on one worker thread, and every request waits its
turn. djox runs those queries on connections that Rust owns and wakes Python
only when the answer is ready.

- **Really async.** No query waits for Django's worker thread.
- **Faster, sync too.** Simple queries skip Django's compiler, and Rust
  builds the model instances.
- **A drop-in.** Two settings. Models, migrations, tests and third-party apps
  keep working.
- **Tested with Django's own tests.** 2,395 of Django's ORM tests are ported,
  2,390 pass.

## Install

```bash
uv add djox  # or: pip install djox
```

- Python 3.11+, Django 5.2, 6.0 or 6.1.
- Wheels for Linux, macOS and Windows: no Rust needed.
- SQLite is bundled. For Postgres, install `"djox[postgres]"`, which adds
  psycopg 3 for Django's sync code.

## Set up

Change two settings:

```python
# settings.py
DATABASES = {
    "default": {
        "ENGINE": "djox.backends.sqlite3",  # or "djox.backends.postgresql"
        "NAME": BASE_DIR / "db.sqlite3",
    }
}
INSTALLED_APPS = [..., "djox"]  # every model's QuerySet now runs on djox
```

## Write async Django as usual

```python
import djox
from django.http import JsonResponse

from shop.models import Book, Order


async def book_detail(request, book_id):
    book = await Book.objects.aget(pk=book_id)  # on djox's connections
    return JsonResponse({"title": book.title, "pages": book.pages})


async def long_books(request):
    titles = [book.title async for book in Book.objects.filter(pages__gt=400)]
    return JsonResponse({"titles": titles})


async def buy(request, book_id):
    async with djox.atomic():  # Django's atomic(), for async code
        book = await Book.objects.aget(pk=book_id)
        await Order.objects.acreate(book=book)
    return JsonResponse({"ok": True})
```

- Anything djox doesn't run itself falls back to Django's own method, so
  nothing breaks.
- Sync code (`Book.objects.get(...)`) keeps working, and simple queries get
  faster too ([the fast lane](fast-lane.md)).

## Next

| To... | Read |
|---|---|
| See which methods run on djox | [Async queries](queries.md) |
| Switch only some models, or none | [Choose the models](models.md) |
| Make simple queries faster | [The fast lane](fast-lane.md) |
| Write transactions in async code | [Transactions](transactions.md) |
| Log, time or tag every statement | [Hooks](hooks.md) |
| Run Django's `TestCase` and pytest | [Testing](testing.md) |
| Use Postgres | [Postgres](postgres.md) |
| Run SQL without Django | [Raw SQL](raw-sql.md) |

!!! note "Status: 0.1.0, the first release"
    Everything in these docs works and is tested on every supported Django,
    on SQLite and Postgres. djox is young, though: expect rough edges, and
    see the [roadmap](roadmap.md) for what's next.
