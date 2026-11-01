"""The exception classes, named like Python's `sqlite3` and Django's `django.db`."""

import pytest

import djox


@pytest.mark.parametrize("error", [djox.IntegrityError, djox.OperationalError, djox.DatabaseClosedError])
def test_every_error_is_a_database_error(error: type[Exception]) -> None:
    assert issubclass(error, djox.DatabaseError)
    assert error.__module__ == "djox"


def test_database_error_is_an_exception() -> None:
    assert issubclass(djox.DatabaseError, Exception)
