"""The endpoints, written once for both stacks (django-bolt finds `api.py` in installed apps).

On the djox stack, `"djox"` in `INSTALLED_APPS` turns the shop models' managers
into djox's, so the same `await Book.objects.aget(...)` runs on djox.
"""

from django_bolt import BoltAPI
from shop.models import Book

api = BoltAPI()


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
    """A page of 20 books with their authors' names."""
    start = (page - 1) * 20
    books = Book.objects.select_related("author").order_by("id")[start : start + 20]
    return [{**book_as_dict(book), "author": book.author.name} async for book in books]
