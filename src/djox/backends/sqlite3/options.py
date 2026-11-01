"""Reads and checks `DATABASES[alias]["OPTIONS"]` for djox's SQLite backend.

| Option             | Default       | What it does                                              |
|--------------------|---------------|-----------------------------------------------------------|
| `timeout`          | `5`           | Seconds to wait for a lock held by another connection      |
| `transaction_mode` | `"IMMEDIATE"` | How `atomic()` starts a transaction: `BEGIN <mode>`        |
| `init_command`     | `""`          | SQL run on every new connection, `;`-separated             |
| `readers`          | 1 per core, at most 4 | Reader connections of the async database (`djox.connections`) |

The first three are Django's own SQLite options. Two things differ:

- `transaction_mode` defaults to `IMMEDIATE`, which takes the write lock at
  `BEGIN`. With `DEFERRED` (SQLite's default), a transaction that reads and
  then writes can fail at once with "database is locked" when another
  connection writes meanwhile, and djox always has more than one connection.
  Set it to `None` for SQLite's plain `BEGIN`.
- An unknown option raises `ImproperlyConfigured` instead of being passed on,
  so a typo (or an option of Python's `sqlite3`, like `detect_types`) is
  caught early.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured

TRANSACTION_MODES = ("DEFERRED", "EXCLUSIVE", "IMMEDIATE")
KNOWN_OPTIONS = ("timeout", "transaction_mode", "init_command", "readers")


@dataclass(frozen=True)
class SqliteOptions:
    """The checked options of one database alias."""

    timeout: float = 5.0
    transaction_mode: str | None = "IMMEDIATE"
    init_commands: tuple[str, ...] = ()
    readers: int | None = None


def read_options(alias: str, options: Mapping[str, object]) -> SqliteOptions:
    """Check `options` (the `OPTIONS` of `alias`) and return them as `SqliteOptions`."""
    unknown = sorted(set(options) - set(KNOWN_OPTIONS))
    if unknown:
        raise ImproperlyConfigured(
            f"settings.DATABASES[{alias!r}]['OPTIONS'] has options djox doesn't know: {', '.join(unknown)}. "
            f"The options are: {', '.join(KNOWN_OPTIONS)}."
        )
    return SqliteOptions(
        timeout=read_timeout(alias, options.get("timeout", 5.0)),
        transaction_mode=read_transaction_mode(alias, options.get("transaction_mode", "IMMEDIATE")),
        init_commands=read_init_commands(alias, options.get("init_command", "")),
        readers=read_readers(alias, options.get("readers")),
    )


def read_timeout(alias: str, timeout: object) -> float:
    # `bool` is a subclass of `int` in Python, so `True` is refused explicitly.
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout < 0:
        raise ImproperlyConfigured(
            f"settings.DATABASES[{alias!r}]['OPTIONS']['timeout'] must be a number of seconds, 0 or more."
        )
    return float(timeout)


def read_transaction_mode(alias: str, transaction_mode: object) -> str | None:
    if transaction_mode is None:
        return None
    if not isinstance(transaction_mode, str) or transaction_mode.upper() not in TRANSACTION_MODES:
        raise ImproperlyConfigured(
            f"settings.DATABASES[{alias!r}]['OPTIONS']['transaction_mode'] is improperly configured to "
            f"'{transaction_mode}'. Use one of {', '.join(repr(mode) for mode in TRANSACTION_MODES)}, or None."
        )
    return transaction_mode.upper()


def read_init_commands(alias: str, init_command: object) -> tuple[str, ...]:
    if not isinstance(init_command, str):
        raise ImproperlyConfigured(f"settings.DATABASES[{alias!r}]['OPTIONS']['init_command'] must be a string.")
    commands = [command.strip() for command in init_command.split(";")]
    return tuple(command for command in commands if command)


def read_readers(alias: str, readers: object) -> int | None:
    if readers is None:
        return None
    if isinstance(readers, bool) or not isinstance(readers, int) or readers < 0:
        raise ImproperlyConfigured(f"settings.DATABASES[{alias!r}]['OPTIONS']['readers'] must be a number, 0 or more.")
    return readers
