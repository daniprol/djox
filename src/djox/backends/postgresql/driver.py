"""Checks that psycopg 3 is installed: the driver of Django's sync code here.

Django's Postgres backend runs on psycopg 3, or on psycopg2 when psycopg 3
is missing. djox needs psycopg 3: Django wraps some parameters for its
driver (JSON in `Jsonb`, bytes in `Binary`), and djox's async queries unwrap
psycopg 3's wrappers only. On psycopg2 those queries would fail later, in
confusing ways, so djox refuses it at once with a clear message.
"""

from django.core.exceptions import ImproperlyConfigured

INSTALL_HINT = 'Install it with `uv add "djox[postgres]"` (or `pip install "djox[postgres]"`).'


def require_psycopg_3() -> None:
    """Raise `ImproperlyConfigured` unless psycopg 3 can be imported."""
    try:
        import psycopg  # noqa: F401  (only checking that it is there)
    except ImportError as error:
        raise ImproperlyConfigured(
            f"djox.backends.postgresql needs psycopg 3 (psycopg2 isn't supported). {INSTALL_HINT}"
        ) from error
