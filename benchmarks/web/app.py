"""A small Django app for the requests-per-second benchmark (`load_test.py`).

    STACK=djox DATABASE=sqlite SERVER=asgi granian --interface asginl app:asgi_app

The same endpoints three times, and the same code on both stacks:

- `/sync/...`: plain Django views (run under WSGI).
- `/async/...`: async Django views (run under ASGI).
- `/drf/...`: a Django REST framework `ModelViewSet` (sync, under WSGI).

`STACK=django` is stock Django. `STACK=djox` changes only the settings: djox's
`ENGINE`, and `"djox"` in `INSTALLED_APPS`, which switches every model to djox
(`DJOX_MODELS`). `DATABASE` is `sqlite` or `postgres` (`DJOX_POSTGRES_URL`).
`SERVER` (`wsgi` or `asgi`) picks how connections are kept between requests.

`python app.py setup` creates the tables and fills them: 100 authors, 10,000 books.
"""

import datetime
import decimal
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import django
from django.conf import settings

STACK = os.environ.get("STACK", "django")  # "django" or "djox"
SERVER = os.environ.get("SERVER", "wsgi")  # "wsgi" or "asgi"
DATABASE = os.environ.get("DATABASE", "sqlite")  # "sqlite" or "postgres"
SQLITE_FILE = Path(os.environ.get("SQLITE_FILE", "/tmp/djox-web-benchmark.sqlite3"))
POSTGRES_URL = os.environ.get("DJOX_POSTGRES_URL", "postgresql://postgres:postgres@localhost/postgres")
PAGE_SIZE = 20
AUTHOR_COUNT = 100
BOOK_COUNT = 10_000


def database_settings() -> dict[str, object]:
    """The `default` database for this stack: only the `ENGINE` differs between stacks."""
    engine_module = "djox.backends" if STACK == "djox" else "django.db.backends"
    if DATABASE == "sqlite":
        return {
            "ENGINE": f"{engine_module}.sqlite3",
            "NAME": SQLITE_FILE,
            # Both stacks use WAL mode (stored in the file at setup) and synchronous=NORMAL, like djox.
            "OPTIONS": {"init_command": "PRAGMA synchronous = NORMAL"},
            # Keep the connection between requests, as in production. Not under ASGI: there each
            # request gets connections of its own, and Django's docs say to turn this off.
            "CONN_MAX_AGE": None if SERVER == "wsgi" else 0,
        }
    url = urlparse(POSTGRES_URL)
    return {
        "ENGINE": f"{engine_module}.postgresql",
        "NAME": "djox_web_benchmark",
        "USER": url.username,
        "PASSWORD": url.password,
        "HOST": url.hostname,
        "PORT": url.port or 5432,
        "OPTIONS": {"pool": True},  # Django's own connection pool (psycopg_pool), as in production
    }


# A whole Django project in one file: no middleware, JSON only.
settings.configure(
    DEBUG=False,
    SECRET_KEY="only-for-the-benchmark",
    ALLOWED_HOSTS=["127.0.0.1", "localhost"],
    ROOT_URLCONF=__name__,
    MIDDLEWARE=[],
    INSTALLED_APPS=["rest_framework", "shop", *(["djox"] if STACK == "djox" else [])],
    DATABASES={"default": database_settings()},
    USE_TZ=True,
    TIME_ZONE="UTC",
    REST_FRAMEWORK={
        "DEFAULT_AUTHENTICATION_CLASSES": [],
        "DEFAULT_PERMISSION_CLASSES": [],
        "UNAUTHENTICATED_USER": None,  # no django.contrib.auth needed
        "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
        "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
        "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
        "PAGE_SIZE": PAGE_SIZE,
    },
)
django.setup()

from django.core.asgi import get_asgi_application  # noqa: E402  (needs the settings above)
from django.core.wsgi import get_wsgi_application  # noqa: E402
from django.db import connection  # noqa: E402
from django.http import HttpRequest, JsonResponse  # noqa: E402
from django.urls import include, path  # noqa: E402
from rest_framework import routers, serializers, viewsets  # noqa: E402
from shop.models import Author, Book  # noqa: E402


def book_as_dict(book: Book) -> dict[str, object]:
    return {
        "id": book.pk,
        "title": book.title,
        "pages": book.pages,
        "price": str(book.price),
        "published": book.published.isoformat(),
        "author_id": book.author_id,
    }


def book_with_author_as_dict(book: Book) -> dict[str, object]:
    return {**book_as_dict(book), "author": book.author.name}


def page_number(request: HttpRequest) -> int:
    return int(request.GET.get("page", "1"))


# ---------------------------------------------------------------- plain sync views


def sync_books(request: HttpRequest) -> JsonResponse:
    """GET: one page of books with their authors (a count and a join). POST: create a book."""
    if request.method == "POST":
        fields = json.loads(request.body)
        book = Book.objects.create(published=datetime.datetime.now(datetime.UTC), **fields)
        return JsonResponse({"id": book.pk}, status=201)
    start = (page_number(request) - 1) * PAGE_SIZE
    books = Book.objects.select_related("author").order_by("id")[start : start + PAGE_SIZE]
    results = [book_with_author_as_dict(book) for book in books]
    return JsonResponse({"count": Book.objects.count(), "results": results})


def sync_book(request: HttpRequest, book_id: int) -> JsonResponse:
    """GET: one book. PATCH: change its pages."""
    if request.method == "PATCH":
        updated = Book.objects.filter(pk=book_id).update(pages=json.loads(request.body)["pages"])
        return JsonResponse({"updated": updated})
    return JsonResponse(book_as_dict(Book.objects.get(pk=book_id)))


# ---------------------------------------------------------------- async views: the same, with `a`-methods


async def async_books(request: HttpRequest) -> JsonResponse:
    if request.method == "POST":
        fields = json.loads(request.body)
        book = await Book.objects.acreate(published=datetime.datetime.now(datetime.UTC), **fields)
        return JsonResponse({"id": book.pk}, status=201)
    start = (page_number(request) - 1) * PAGE_SIZE
    books = Book.objects.select_related("author").order_by("id")[start : start + PAGE_SIZE]
    results = [book_with_author_as_dict(book) async for book in books]
    return JsonResponse({"count": await Book.objects.acount(), "results": results})


async def async_book(request: HttpRequest, book_id: int) -> JsonResponse:
    if request.method == "PATCH":
        updated = await Book.objects.filter(pk=book_id).aupdate(pages=json.loads(request.body)["pages"])
        return JsonResponse({"updated": updated})
    return JsonResponse(book_as_dict(await Book.objects.aget(pk=book_id)))


# ---------------------------------------------------------------- Django REST framework


class BookSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.name", read_only=True)

    class Meta:
        model = Book
        fields = ("id", "title", "pages", "price", "published", "author", "author_name")
        read_only_fields = ("published",)

    def create(self, validated_data: dict[str, object]) -> Book:
        return Book.objects.create(published=datetime.datetime.now(datetime.UTC), **validated_data)


class BookViewSet(viewsets.ModelViewSet):
    queryset = Book.objects.select_related("author").order_by("id")
    serializer_class = BookSerializer


router = routers.SimpleRouter()
router.register("books", BookViewSet)

urlpatterns = [
    path("sync/books", sync_books),
    path("sync/books/<int:book_id>", sync_book),
    path("async/books", async_books),
    path("async/books/<int:book_id>", async_book),
    path("drf/", include(router.urls)),
]

wsgi_app = get_wsgi_application()
asgi_app = get_asgi_application()


# ---------------------------------------------------------------- setup


def setup() -> None:
    """Create fresh tables and fill them."""
    if DATABASE == "sqlite":
        for suffix in ["", "-wal", "-shm"]:
            Path(f"{SQLITE_FILE}{suffix}").unlink(missing_ok=True)
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA journal_mode = WAL")  # stored in the file
    else:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS shop_book, shop_author")
    with connection.schema_editor() as editor:
        editor.create_model(Author)
        editor.create_model(Book)
    authors = Author.objects.bulk_create(Author(name=f"Author {number}") for number in range(AUTHOR_COUNT))
    published = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    Book.objects.bulk_create(
        Book(
            title=f"Book {number}",
            pages=number % 1000,
            price=decimal.Decimal("9.99"),
            published=published,
            author=authors[number % AUTHOR_COUNT],
        )
        for number in range(BOOK_COUNT)
    )
    connection.close()


if __name__ == "__main__" and sys.argv[1:] == ["setup"]:
    setup()
