<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/daniprol/djox/main/docs/assets/logo-wordmark-dark.svg">
    <img alt="djox" src="https://raw.githubusercontent.com/daniprol/djox/main/docs/assets/logo-wordmark-light.svg" height="64">
  </picture>
</p>

<p align="center">
  <strong>Truly async Django ORM, with a Rust core.</strong><br>
  Keep your models, migrations and admin. Make <code>await Book.objects.aget(pk=1)</code> really async, and fast.
</p>

<p align="center">
  <a href="https://pypi.org/project/djox/"><img alt="PyPI" src="https://img.shields.io/pypi/v/djox?color=44B78B"></a>
  <a href="https://github.com/daniprol/djox/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/daniprol/djox/ci.yml?branch=main&label=CI"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB">
  <img alt="Django 5.2 to 6.1" src="https://img.shields.io/badge/django-5.2%20%7C%206.0%20%7C%206.1-0C4B33">
  <img alt="SQLite and Postgres" src="https://img.shields.io/badge/databases-SQLite%20%7C%20Postgres-64748B">
  <a href="https://github.com/daniprol/djox/blob/main/LICENSE"><img alt="MIT license" src="https://img.shields.io/github/license/daniprol/djox?color=64748B"></a>
</p>

<p align="center">
  <a href="https://daniprol.github.io/djox/"><strong>Documentation</strong></a> ·
  <a href="#quickstart">Quickstart</a> ·
  <a href="https://daniprol.github.io/djox/benchmarks/">Benchmarks</a> ·
  <a href="https://daniprol.github.io/djox/how-it-works/">How it works</a> ·
  <a href="https://daniprol.github.io/djox/changelog/">Changelog</a>
</p>

---

Django's async ORM isn't async underneath: `aget()`, `acount()` and `async for` run the sync ORM on one worker
thread, and every request waits its turn for it. djox runs those queries on connections that Rust owns and wakes
Python only when the answer is ready. Django keeps doing what it does well (models, migrations, the admin, the
QuerySet language), so your code doesn't change.

- **Really async.** No query waits for Django's worker thread, so async views scale with concurrent requests.
- **Faster, sync too.** Simple queries skip Django's query compiler, and Rust builds the model instances.
- **A drop-in.** Two settings. Your models, migrations, tests and third-party apps keep working.
- **Tested with Django's own tests.** 2,395 of Django's ORM tests are ported to djox, and 2,390 pass, on every
  supported Django.

## Speed

The same Django app on stock Django and on djox: one worker process, SQLite, requests per second, higher is better
([method, Postgres and every number](https://daniprol.github.io/djox/benchmarks/#web-app-requests-per-second)).

| | Django | djox |
|---|---:|---:|
| Sync view, get one book | 1,629 | **2,451** (1.5x) |
| Async view, get one book | 168 | **416** (2.5x) |
| Async view, a page of 20 books with their authors | 76 | **235** (3.1x) |
| DRF viewset, get one book | 809 | **1,026** (1.3x) |

- **Async views gain the most:** 1.4 to 3.1 times the requests, on SQLite and Postgres.
- **One ORM call** ([all of them](https://daniprol.github.io/djox/benchmarks/#orm-calls-on-sqlite)): `await Book.objects.aget()` takes
  132 µs against Django's 507, and a sync `get()` 61 against 216.
- **DRF gains less** (1.0 to 1.3 times): most of a DRF request is DRF's own code.

## Quickstart

```bash
uv add djox  # or: pip install djox
uv add "djox[postgres]"  # on Postgres: adds psycopg 3 for Django's sync code
```

Python 3.11+, Django 5.2 to 6.1. Wheels for Linux, macOS and Windows, so no Rust needed.

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

Then write async Django as usual:

```python
import djox
from django.http import JsonResponse

from shop.models import Book, Order


async def book_detail(request, book_id):
    book = await Book.objects.aget(pk=book_id)  # on djox's connections, not Django's worker thread
    return JsonResponse({"title": book.title, "pages": book.pages})


async def long_books(request):
    titles = [book.title async for book in Book.objects.filter(pages__gt=400).order_by("title")]
    return JsonResponse({"titles": titles})


async def buy(request, book_id):
    async with djox.atomic():  # Django's atomic(), for async code
        book = await Book.objects.aget(pk=book_id)
        await Order.objects.acreate(book=book)
    return JsonResponse({"ok": True})
```

Anything djox doesn't run itself falls back to Django's own method, so nothing breaks. Next:
[the documentation](https://daniprol.github.io/djox/).

## What you get

| | |
|---|---|
| [Async QuerySets](https://daniprol.github.io/djox/queries/) | `aget`, `acount`, `async for`, `acreate`, `aupdate`, `adelete`, `aget_or_create`... on djox |
| [The fast lane](https://daniprol.github.io/djox/fast-lane/) | Simple queries, sync or async, reuse the SQL Django wrote once, and Rust builds the results |
| [Existing models](https://daniprol.github.io/djox/models/) | `DJOX_MODELS` switches any model, `User` included, without touching its code |
| [Async transactions](https://daniprol.github.io/djox/transactions/) | `async with djox.atomic():` and `djox.on_commit()` |
| [Streaming](https://daniprol.github.io/djox/streaming/) | `aiterator()` reads big results a chunk at a time |
| [Hooks](https://daniprol.github.io/djox/hooks/) | Plain functions called before and after every statement, sync or async |
| [Postgres](https://daniprol.github.io/djox/postgres/) | `djox.backends.postgresql`: a pool of Rust connections for async code |
| [Raw SQL](https://daniprol.github.io/djox/raw-sql/) | `djox.Database`: async SQL on SQLite or Postgres, without Django |

> **Status: 0.1.0, the first release.** Everything above works and is tested on every supported Django, on SQLite
> and Postgres. djox is young, though: expect rough edges, and check the [roadmap](https://daniprol.github.io/djox/roadmap/).

## How it works

```text
await Book.objects.aget(pk=1)
  │  Django writes the SQL (once per query shape, then djox reuses it)
  ▼
djox ──► a Rust thread with its own connection (SQLite: one writer, a few readers; Postgres: a pool)
  │  Rust converts the rows and builds the Book
  ▼
your coroutine wakes up with the answer; the event loop never blocked
```

The Rust code is written for Python developers who have never seen Rust: plain names, straight-line code, and
comments that explain each Rust feature and each database detail. Start with [How it works](https://daniprol.github.io/djox/how-it-works/)
and [AGENTS.md](https://github.com/daniprol/djox/blob/main/AGENTS.md).

## Develop

```bash
uv sync                              # builds the Rust extension
uv run pytest                        # Python tests, Django's ported tests included
scripts/cargo-test.sh --workspace    # Rust tests
uv run prek install                  # git hooks: ruff, mypy, pyrefly, cargo fmt, clippy, tests
uv run --only-group docs zensical serve   # the docs, on http://localhost:8000
```

## License

MIT
