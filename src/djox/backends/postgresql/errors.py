"""Turning both drivers' errors into Django's exceptions.

Django's cursors run every statement inside `wrap_database_errors`, which
turns the driver's exceptions into Django's (`psycopg.IntegrityError` into
`django.db.IntegrityError`...). It knows one driver: the module in
`DatabaseWrapper.Database`, psycopg on Postgres. djox's async queries run in
the same block but raise djox's exceptions, so this wrapper turns those into
Django's too, by the same names.
"""

from types import TracebackType

from django.db import utils

import djox

# Django's exceptions, most specific first, as Django's own wrapper lists them.
DJANGO_ERRORS: tuple[type[utils.Error], ...] = (
    utils.DataError,
    utils.OperationalError,
    utils.IntegrityError,
    utils.InternalError,
    utils.ProgrammingError,
    utils.NotSupportedError,
    utils.DatabaseError,
    utils.InterfaceError,
    utils.Error,
)


class DatabaseErrors(utils.DatabaseErrorWrapper):
    """Django's `wrap_database_errors`, for psycopg's errors and for djox's."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,  # Django's names for these three
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        exception_type, exception = exc_type, exc_value
        if exception_type is not None and issubclass(exception_type, djox.Error):
            for django_error in DJANGO_ERRORS:
                # `djox.IntegrityError` for `django.db.IntegrityError`, and so on.
                if issubclass(exception_type, getattr(djox, django_error.__name__)):
                    assert exception is not None, "Python passes the exception with its type"
                    raise django_error(*exception.args).with_traceback(traceback) from exception
        super().__exit__(exception_type, exception, traceback)
