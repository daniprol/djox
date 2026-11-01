"""`DATABASES[alias]["OPTIONS"]` for djox's ENGINE: defaults, and clear errors."""

import pytest
from django.core.exceptions import ImproperlyConfigured

from djox.backends.sqlite3.options import SqliteOptions, read_options


def test_defaults() -> None:
    assert read_options("default", {}) == SqliteOptions(
        timeout=5.0, transaction_mode="IMMEDIATE", init_commands=(), readers=None
    )


def test_djangos_options_are_read() -> None:
    options = read_options(
        "default",
        {"timeout": 20, "transaction_mode": "deferred", "init_command": "PRAGMA cache_size=2000; ;", "readers": 2},
    )
    assert options == SqliteOptions(
        timeout=20.0, transaction_mode="DEFERRED", init_commands=("PRAGMA cache_size=2000",), readers=2
    )


def test_transaction_mode_none_means_a_plain_begin() -> None:
    assert read_options("default", {"transaction_mode": None}).transaction_mode is None


def test_unknown_options_are_refused() -> None:
    with pytest.raises(ImproperlyConfigured, match="doesn't know: detect_types"):
        read_options("default", {"detect_types": 3})


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"timeout": -1}, "timeout"),
        ({"timeout": True}, "timeout"),
        ({"transaction_mode": "LAZY"}, "transaction_mode"),
        ({"init_command": ["PRAGMA x"]}, "init_command"),
        ({"readers": -1}, "readers"),
    ],
)
def test_invalid_options_name_the_option(options: dict[str, object], message: str) -> None:
    with pytest.raises(ImproperlyConfigured, match=message):
        read_options("default", options)
