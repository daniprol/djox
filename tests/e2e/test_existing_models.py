"""Existing models switched to djox by `"djox"` in `INSTALLED_APPS` and `DJOX_MODELS`.

`tests.e2e.shelf` is written as if djox didn't exist, and so is Django's
`auth` app. `tests/settings.py` lists both in `DJOX_MODELS`.
"""

import pickle
import threading
from io import StringIO
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db.models.signals import m2m_changed, pre_save

import djox
from tests.e2e.library.models import Author
from tests.e2e.shelf.models import Box, Label, Shelf, ShelfQuerySet

pytestmark = pytest.mark.django_db(transaction=True)


class ThreadNotes:
    """Notes the thread each signal of `signal` runs on, while used as a `with` block."""

    def __init__(self, signal: Any, sender: type) -> None:
        self.signal, self.sender = signal, sender
        self.names: list[str] = []

    def note(self, **kwargs: Any) -> None:
        self.names.append(threading.current_thread().name)

    def __enter__(self) -> list[str]:
        self.signal.connect(self.note, sender=self.sender, weak=False)
        return self.names

    def __exit__(self, *exception: object) -> None:
        self.signal.disconnect(self.note, sender=self.sender)


def all_on_djox_threads(names: list[str]) -> bool:
    return bool(names) and all(name.startswith("djox") for name in names)


async def test_queries_run_on_djox_and_the_custom_queryset_still_works() -> None:
    await Shelf.objects.acreate(name="Hall", width=120)
    await Shelf.objects.acreate(name="Desk", width=60)
    queryset = Shelf.objects.wide()
    assert isinstance(queryset, ShelfQuerySet), "the model's own QuerySet"
    assert isinstance(queryset, djox.QuerySet), "with djox's async methods"
    assert [shelf.name async for shelf in queryset] == ["Hall"]


async def test_model_methods_run_on_djox_threads() -> None:
    with ThreadNotes(pre_save, Shelf) as names:
        shelf = Shelf(name="Hall")
        await shelf.asave()
        shelf.width = 200
        await shelf.asave(update_fields=["width"])
    assert all_on_djox_threads(names), names
    await shelf.arefresh_from_db()
    assert await shelf.adelete() == (1, {"shelf.Shelf": 1})


async def test_related_managers_run_on_djox_threads() -> None:
    shelf = await Shelf.objects.acreate(name="Hall")
    with ThreadNotes(pre_save, Box) as names:
        await shelf.boxes.acreate(contents="Books")
    assert all_on_djox_threads(names), names

    red, blue = await Label.objects.acreate(text="red"), await Label.objects.acreate(text="blue")
    with ThreadNotes(m2m_changed, Shelf.labels.through) as names:
        await shelf.labels.aadd(red, blue)
        await shelf.labels.aremove(red)
        await shelf.labels.aset([red])
    assert all_on_djox_threads(names), names
    assert [label.text async for label in shelf.labels.all()] == ["red"]
    await shelf.labels.aclear()
    assert await shelf.labels.acount() == 0


async def test_a_librarys_models_run_on_djox_too() -> None:
    with ThreadNotes(pre_save, User) as names:
        user, created = await User.objects.aget_or_create(username="dani")
        user.first_name = "Dani"
        await user.asave()
    assert created
    assert all_on_djox_threads(names), names
    assert (await User.objects.aget(username="dani")).first_name == "Dani"


def test_a_switched_queryset_can_be_pickled() -> None:
    # Cached querysets are pickled: the class must be found again.
    queryset = Shelf.objects.wide()
    again = pickle.loads(pickle.dumps(queryset))
    assert type(again) is type(queryset)


@pytest.mark.skipif(
    "auth" in getattr(settings, "MIGRATION_MODULES", {}), reason="auth's migrations are off (Postgres test settings)"
)
def test_migrations_dont_see_djox() -> None:
    # `auth`'s `UserManager` is written down in its migrations: switching it
    # to djox must not look like a change.
    call_command("makemigrations", "auth", check=True, dry_run=True, stdout=StringIO())


def test_models_that_already_use_djox_are_left_as_they_are() -> None:
    # `Author` is a `djox.Model` already: its manager class is djox's own.
    assert type(Author.objects) is djox.Manager
    assert Author.asave is djox.Model.asave
