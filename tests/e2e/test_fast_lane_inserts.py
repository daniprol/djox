"""New objects on the fast lane: `create()`, `acreate()` and `asave()`.

The first new object of a model is saved Django's way, and the fast lane
keeps the `INSERT` Django wrote; the next ones reuse it. Each test checks
that the objects, the rows and the signals are Django's (computed with the
fast lane off, `djangos()`), and that the fast lane really ran.
"""

import datetime
import decimal
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.db import IntegrityError, connection, transaction
from django.db.models.signals import post_save, pre_save
from django.test.utils import CaptureQueriesContext

import djox
from djox._fast_lane import inserts
from djox._fast_lane.inserts import InsertStatement
from tests.e2e.kinds.models import Shout, Ticket
from tests.e2e.library.models import Author, Book
from tests.e2e.test_fast_lane import PUBLISHED, djangos, fields_of

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def no_statements_yet() -> Iterator[None]:
    """Each test starts with the fast lane knowing no `INSERT` yet."""
    inserts.statements.clear()
    yield
    inserts.statements.clear()


def kept_statements() -> list[InsertStatement]:
    """The `INSERT`s the fast lane kept (models it inserts itself)."""
    return [found for found in inserts.statements.values() if isinstance(found, InsertStatement)]


def book_values(author: Author, title: str = "Dune") -> dict[str, Any]:
    return {"title": title, "pages": 412, "price": decimal.Decimal("9.99"), "published": PUBLISHED, "author": author}


def without_id(obj: Any) -> dict[str, Any]:
    """An object's fields and state, but not its id (each new row has its own)."""
    fields = fields_of(obj)
    fields.pop("id")
    return fields


def test_create_learns_the_insert_then_reuses_it() -> None:
    author = Author.objects.create(name="Frank Herbert")
    with CaptureQueriesContext(connection) as captured:
        first = Book.objects.create(**book_values(author))  # Django's way, learned
        second = Book.objects.create(**book_values(author, "Dune Messiah"))  # the fast lane's
    assert [query["sql"].split(" VALUES ")[0] for query in captured.captured_queries] == [
        captured.captured_queries[0]["sql"].split(" VALUES ")[0]
    ] * 2, "the same SQL"
    assert {statement.fields[0].model for statement in kept_statements()} == {Author, Book}
    assert without_id(second) == {**without_id(first), "title": "Dune Messiah"}
    assert fields_of(second) == fields_of(Book.objects.get(pk=second.pk)), "the row is what the object says"


def test_created_objects_are_djangos() -> None:
    author = Author.objects.create(name="Frank Herbert")

    def create_twice() -> list[dict[str, Any]]:
        return [without_id(Book.objects.create(**book_values(author))) for _ in range(2)]

    assert create_twice() == djangos(create_twice)


async def test_acreate_inserts_on_the_fast_lane() -> None:
    author = await Author.objects.acreate(name="Frank Herbert")
    first = await Book.objects.acreate(**book_values(author))
    second = await Book.objects.acreate(**book_values(author, "Dune Messiah"))
    assert {statement.fields[0].model for statement in kept_statements()} == {Author, Book}
    assert without_id(second) == {**without_id(first), "title": "Dune Messiah"}
    stored = await Book.objects.aget(pk=second.pk)
    assert fields_of(second) == fields_of(stored)


async def test_asave_of_a_new_object_inserts_on_the_fast_lane() -> None:
    for name in ("Jane Austen", "Mary Shelley"):
        author = Author(name=name)
        await author.asave()
        assert (author._state.adding, author._state.db) == (False, "default")
        assert author.pk is not None
    assert len(kept_statements()) == 1
    assert [name async for name in Author.objects.order_by("pk").values_list("name", flat=True)] == [
        "Jane Austen",
        "Mary Shelley",
    ]


async def test_asave_with_a_primary_key() -> None:
    forced = Author(id=1000, name="Forced")
    await forced.asave(force_insert=True)  # an `INSERT` with the id: the fast lane's
    asked = Author(id=1001, name="Asked")
    await asked.asave()  # Django tries an `UPDATE` first, then inserts
    assert {author.pk: author.name async for author in Author.objects.all()} == {1000: "Forced", 1001: "Asked"}


class SignalNotes:
    """Async receivers of `pre_save` and `post_save` that note what they were sent."""

    def __init__(self) -> None:
        self.notes: list[tuple[Any, ...]] = []

    async def note(self, signal: Any, sender: Any, instance: Any, **details: Any) -> None:
        name = "pre_save" if signal is pre_save else "post_save"
        self.notes.append((name, sender, instance.pk is not None, sorted(details.items())))


async def test_signals_are_sent_like_django() -> None:
    author = await Author.objects.acreate(name="Frank Herbert")
    notes = SignalNotes()
    pre_save.connect(notes.note, sender=Book, weak=False)
    post_save.connect(notes.note, sender=Book, weak=False)
    try:
        await sync_to_async(djangos)(lambda: Book.objects.create(**book_values(author)))
        djangos_notes, notes.notes = notes.notes, []
        await Book.objects.acreate(**book_values(author))  # Django's way, learned
        await Book.objects.acreate(**book_values(author))  # the fast lane's
    finally:
        pre_save.disconnect(notes.note, sender=Book)
        post_save.disconnect(notes.note, sender=Book)
    assert notes.notes == djangos_notes * 2
    assert kept_statements(), "the fast lane ran, async receivers and all"


def test_values_django_fills_are_filled() -> None:
    before = datetime.datetime.now(datetime.UTC)
    tickets = [Ticket.objects.create(title=f"Ticket {number}", priority=1) for number in range(2)]
    assert [statement.fields[0].model for statement in kept_statements()] == [Ticket]
    for ticket in tickets:
        assert isinstance(ticket.pk, uuid.UUID)
        assert before <= ticket.opened <= ticket.changed
        stored = Ticket.objects.get(pk=ticket.pk)
        assert (stored.opened, stored.changed, stored.day, stored.priority) == (
            ticket.opened,
            ticket.changed,
            ticket.day,
            1,
        )


def test_a_value_from_the_database_is_djangos_way() -> None:
    # `priority` isn't given: the database gives it (`db_default`), which is other SQL.
    def create() -> dict[str, Any]:
        ticket = Ticket.objects.create(title="Ticket", priority=1)
        ticket = Ticket.objects.create(title="Ticket")
        fields = {name: value for name, value in fields_of(ticket).items() if name in ("title", "priority")}
        return {**fields, "stored": Ticket.objects.get(pk=ticket.pk).priority}

    assert create() == djangos(create)
    assert create()["stored"] == 3


async def test_a_models_own_save_is_called() -> None:
    assert (await Shout.objects.acreate(text="hello")).text == "HELLO"
    assert (await sync_to_async(lambda: Shout.objects.create(text="again"))()).text == "AGAIN"
    assert [text async for text in Shout.objects.order_by("pk").values_list("text", flat=True)] == ["HELLO", "AGAIN"]
    assert kept_statements() == [], "Django's create() ran, with the model's save()"


def test_errors_are_djangos_and_mark_the_transaction_for_rollback() -> None:
    author = Author.objects.create(name="Frank Herbert")
    Book.objects.create(**book_values(author))  # the statement is learned
    with transaction.atomic():
        with pytest.raises(IntegrityError):
            Book.objects.create(**{**book_values(author), "published": None})
        assert transaction.get_rollback()
        transaction.set_rollback(False)
    with pytest.raises(ValueError, match="unsaved related object 'author'"):
        Book.objects.create(**book_values(Author(name="Not saved")))


async def test_async_errors_are_djangos() -> None:
    author = await Author.objects.acreate(name="Frank Herbert")
    await Book.objects.acreate(**book_values(author))  # the statement is learned
    with pytest.raises(IntegrityError):
        async with djox.atomic():
            await Book.objects.acreate(**{**book_values(author), "published": None})
    assert await Book.objects.acount() == 1


def test_the_fast_lane_can_be_turned_off() -> None:
    author = Author.objects.create(name="Frank Herbert")
    djangos(lambda: [Book.objects.create(**book_values(author)) for _ in range(2)])
    assert [statement.fields[0].model for statement in kept_statements()] == [Author]
