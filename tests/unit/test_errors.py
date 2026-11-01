"""The exception classes follow PEP 249, like Python's `sqlite3`, so Django can
turn each one into its own `django.db` exception of the same name."""

import sqlite3

import pytest

import djox

PEP_249_NAMES = [
    "Error",
    "InterfaceError",
    "DatabaseError",
    "DataError",
    "OperationalError",
    "IntegrityError",
    "InternalError",
    "ProgrammingError",
    "NotSupportedError",
]


@pytest.mark.parametrize("name", PEP_249_NAMES)
def test_errors_are_shaped_like_sqlite3s(name: str) -> None:
    djox_error: type[Exception] = getattr(djox, name)
    sqlite3_error: type[Exception] = getattr(sqlite3, name)
    djox_parents = [parent.__name__ for parent in djox_error.__mro__]
    sqlite3_parents = [parent.__name__ for parent in sqlite3_error.__mro__]
    assert djox_parents == sqlite3_parents
    assert djox_error.__module__ == "djox"


def test_a_closed_database_is_an_interface_error() -> None:
    assert issubclass(djox.DatabaseClosedError, djox.InterfaceError)


def test_every_error_is_an_exception() -> None:
    assert issubclass(djox.Error, Exception)
