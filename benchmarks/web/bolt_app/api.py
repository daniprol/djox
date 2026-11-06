"""The benchmark's endpoints, in django-bolt: an async handler per endpoint.

django-bolt finds this module (`api.py` of an installed app) and serves `api`.
Each handler is `async`, so its queries are awaited, as in the web app's async views:

- `GET /books/{book_id}`: one book, `aget()`.
- `GET /books?page=N`: a page of 20 books with their authors, `async for` over a join.
- `POST /books`: create a book, `acreate()`, answered with status 201.
- `POST /books/{book_id}/read`: count a read, in one transaction: `get()`, an `UPDATE` with `F()`,
  and an `INSERT` of a read log row. Stock Django has no async transaction: its block runs on
  Django's sync code in a thread (`sync_to_async`). djox's `atomic()` is async.
"""

import datetime
import decimal

import msgspec
from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import F
from django_bolt import BoltAPI
from shop.models import Book, Read

import djox
from bolt_app.settings import STACK

PAGE_SIZE = 20

api = BoltAPI()


class NewBook(msgspec.Struct):
    """The JSON body of `POST /books`. msgspec checks and converts it, as bolt does for every body."""

    title: str
    pages: int
    price: str
    author_id: int


def book_as_dict(book: Book) -> dict[str, object]:
    return {
        "id": book.pk,
        "title": book.title,
        "pages": book.pages,
        "price": str(book.price),
        "published": book.published.isoformat(),
        "author_id": book.author_id,
    }


@api.get("/books/{book_id}")
async def get_book(book_id: int) -> dict[str, object]:
    return book_as_dict(await Book.objects.aget(pk=book_id))


@api.get("/books")
async def list_books(page: int = 1) -> list[dict[str, object]]:
    start = (page - 1) * PAGE_SIZE
    books = Book.objects.select_related("author").order_by("id")[start : start + PAGE_SIZE]
    return [{**book_as_dict(book), "author": book.author.name} async for book in books]


@api.post("/books", status_code=201)
async def create_book(payload: NewBook) -> dict[str, object]:
    book = await Book.objects.acreate(
        title=payload.title,
        pages=payload.pages,
        price=decimal.Decimal(payload.price),
        published=datetime.datetime.now(datetime.UTC),
        author_id=payload.author_id,
    )
    return {"id": book.pk}


def count_read_django(book_id: int) -> int:
    """Stock Django: one sync transaction. Django's own `atomic()` runs on the thread it is called on."""
    with transaction.atomic():
        book = Book.objects.get(pk=book_id)
        Book.objects.filter(pk=book.pk).update(read_count=F("read_count") + 1)
        return Read.objects.create(book=book, read_at=datetime.datetime.now(datetime.UTC)).pk


async def count_read_djox(book_id: int) -> int:
    """djox: the same transaction, awaited. `djox.atomic()` runs its statements on djox's connection."""
    async with djox.atomic():
        book = await Book.objects.aget(pk=book_id)
        await Book.objects.filter(pk=book.pk).aupdate(read_count=F("read_count") + 1)
        read = await Read.objects.acreate(book=book, read_at=datetime.datetime.now(datetime.UTC))
    return read.pk


# The same transaction on each stack: djox's is async, Django's runs in a thread.
count_read = count_read_djox if STACK == "djox" else sync_to_async(count_read_django)


@api.post("/books/{book_id}/read", status_code=201)
async def read_book(book_id: int) -> dict[str, object]:
    return {"id": await count_read(book_id)}
