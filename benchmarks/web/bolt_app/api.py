"""The benchmark's endpoints, in django-bolt: an async handler per endpoint.

django-bolt finds this module (`api.py` of an installed app) and serves `api`.
Each handler is `async`, so its queries are awaited, as in the web app's async views:

- `GET /books/{book_id}`: one book, `aget()`.
- `GET /books?page=N`: a page of 20 books with their authors, `async for` over a join.
- `POST /books`: create a book, `acreate()`, answered with status 201.
"""

import datetime
import decimal

import msgspec
from django_bolt import BoltAPI
from shop.models import Book

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
