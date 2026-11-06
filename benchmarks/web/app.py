"""A small Django app for the requests-per-second benchmark (`load_test.py`).

    STACK=djox DATABASE=sqlite SERVER=asgi granian --interface asginl app:asgi_app

The same endpoints three times, and the same code on both stacks:

- books: get one, list a page with authors, update, create;
- authors with their books' stats (`annotate(Count(), Sum())`), one author's
  stats (`aggregate()`), and counting a read (`update(read_count=F(...) + 1)`):
  the queries of a dashboard or a profile page.


- `/sync/...`: plain Django views (run under WSGI).
- `/async/...`: async Django views (run under ASGI).
- `/drf/...`: a Django REST framework `ModelViewSet` (sync, under WSGI).

`STACK=django` is stock Django. `STACK=djox` changes only the settings: djox's
`ENGINE`, and `"djox"` in `INSTALLED_APPS`, which switches every model to djox
(`DJOX_MODELS`). `DATABASE` is `sqlite` or `postgres` (`DJOX_POSTGRES_URL`).
`SERVER` (`wsgi` or `asgi`) picks how connections are kept between requests.
Under ASGI, djox serves with its own handler (`djox.asgi`), as its docs advise
(`HANDLER=django` or `djox` picks one by hand).

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
HANDLER = os.environ.get("HANDLER", STACK)  # the ASGI handler: "django" or "djox" (`djox.asgi`)
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
            # Keep the connection between requests, as in production. Not under Django's ASGI handler:
            # there each request gets a new thread, so new connections, and Django's docs say to turn
            # this off. djox's handler runs sync code on pooled threads, which keep theirs.
            "CONN_MAX_AGE": None if SERVER == "wsgi" or HANDLER == "djox" else 0,
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
from django.db.models import Avg, Count, F, Max, Sum  # noqa: E402
from django.http import HttpRequest, JsonResponse  # noqa: E402
from django.urls import include, path  # noqa: E402
from rest_framework import decorators, response, routers, serializers, viewsets  # noqa: E402
from shop.models import Author, Book, Read  # noqa: E402


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


def authors_with_stats() -> object:
    """Authors with how many books they wrote and their total pages: a `GROUP BY` and a join."""
    return Author.objects.annotate(book_count=Count("books"), total_pages=Sum("books__pages")).order_by("id")


def author_with_stats_as_dict(author: Author) -> dict[str, object]:
    return {
        "id": author.pk,
        "name": author.name,
        "book_count": author.book_count,  # type: ignore[attr-defined]  # an annotation
        "total_pages": author.total_pages,  # type: ignore[attr-defined]
    }


def books_stats(author_id: int) -> object:
    """The books of one author, for `aggregate()`."""
    return Book.objects.filter(author_id=author_id)


STATS = {"average_pages": Avg("pages"), "longest": Max("pages"), "total_price": Sum("price")}


def stats_as_dict(author_id: int, stats: dict[str, object]) -> dict[str, object]:
    return {"author_id": author_id, **{name: str(value) for name, value in stats.items()}}


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


def sync_authors(request: HttpRequest) -> JsonResponse:
    """GET: one page of authors with their books' stats."""
    start = (page_number(request) - 1) * PAGE_SIZE
    authors = authors_with_stats()[start : start + PAGE_SIZE]
    results = [author_with_stats_as_dict(author) for author in authors]
    return JsonResponse({"count": Author.objects.count(), "results": results})


def sync_author_stats(request: HttpRequest, author_id: int) -> JsonResponse:
    """GET: the stats of one author's books."""
    return JsonResponse(stats_as_dict(author_id, books_stats(author_id).aggregate(**STATS)))


def sync_read_book(request: HttpRequest, book_id: int) -> JsonResponse:
    """POST: count one more read of a book."""
    updated = Book.objects.filter(pk=book_id).update(read_count=F("read_count") + 1)
    return JsonResponse({"updated": updated})


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


async def async_authors(request: HttpRequest) -> JsonResponse:
    start = (page_number(request) - 1) * PAGE_SIZE
    authors = authors_with_stats()[start : start + PAGE_SIZE]
    results = [author_with_stats_as_dict(author) async for author in authors]
    return JsonResponse({"count": await Author.objects.acount(), "results": results})


async def async_author_stats(request: HttpRequest, author_id: int) -> JsonResponse:
    return JsonResponse(stats_as_dict(author_id, await books_stats(author_id).aaggregate(**STATS)))


async def async_read_book(request: HttpRequest, book_id: int) -> JsonResponse:
    updated = await Book.objects.filter(pk=book_id).aupdate(read_count=F("read_count") + 1)
    return JsonResponse({"updated": updated})


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

    @decorators.action(detail=True, methods=["post"])
    def read(self, request: HttpRequest, pk: str) -> response.Response:
        updated = Book.objects.filter(pk=pk).update(read_count=F("read_count") + 1)
        return response.Response({"updated": updated})


class AuthorWithStatsSerializer(serializers.ModelSerializer):
    book_count = serializers.IntegerField(read_only=True)
    total_pages = serializers.IntegerField(read_only=True)

    class Meta:
        model = Author
        fields = ("id", "name", "book_count", "total_pages")


class AuthorViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = authors_with_stats()
    serializer_class = AuthorWithStatsSerializer

    @decorators.action(detail=True)
    def stats(self, request: HttpRequest, pk: str) -> response.Response:
        return response.Response(stats_as_dict(int(pk), books_stats(int(pk)).aggregate(**STATS)))


router = routers.SimpleRouter()
router.register("books", BookViewSet)
router.register("authors", AuthorViewSet)

urlpatterns = [
    path("sync/books", sync_books),
    path("sync/books/<int:book_id>", sync_book),
    path("async/books", async_books),
    path("async/books/<int:book_id>", async_book),
    path("sync/authors", sync_authors),
    path("sync/authors/<int:author_id>/stats", sync_author_stats),
    path("sync/books/<int:book_id>/read", sync_read_book),
    path("async/authors", async_authors),
    path("async/authors/<int:author_id>/stats", async_author_stats),
    path("async/books/<int:book_id>/read", async_read_book),
    path("drf/", include(router.urls)),
]

wsgi_app = get_wsgi_application()
if HANDLER == "djox":
    from djox.asgi import get_asgi_application as get_djox_asgi_application

    asgi_app = get_djox_asgi_application()
else:
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
            cursor.execute("DROP TABLE IF EXISTS shop_read, shop_book, shop_author")
    with connection.schema_editor() as editor:
        editor.create_model(Author)
        editor.create_model(Book)
        editor.create_model(Read)
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
