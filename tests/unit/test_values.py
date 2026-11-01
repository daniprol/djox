"""How Python values become database values and back (the rules in `src/values.rs`)."""

import datetime
import decimal
import uuid

import pytest

import djox


async def test_values_are_stored_like_djangos_sqlite_backend(database: djox.Database) -> None:
    moment = datetime.datetime(2026, 11, 1, 9, 30, 15, tzinfo=datetime.UTC)
    identifier = uuid.UUID("12345678-1234-5678-1234-567812345678")
    parameters = [
        None,
        True,
        42,
        1.5,
        "Dune",
        b"\x00\xff",
        bytearray(b"ab"),
        memoryview(b"cd"),
        decimal.Decimal("12.50"),
        moment,
        datetime.date(2026, 11, 1),
        datetime.time(9, 30),
        identifier,
    ]
    placeholders = ", ".join(["%s"] * len(parameters))

    row = await database.fetch_one(f"SELECT {placeholders}", parameters)

    assert row == (
        None,
        1,
        42,
        1.5,
        "Dune",
        b"\x00\xff",
        b"ab",
        b"cd",
        "12.50",
        "2026-11-01 09:30:15+00:00",
        "2026-11-01",
        "09:30:00",
        str(identifier),
    )


async def test_values_that_cannot_be_stored_raise(database: djox.Database) -> None:
    with pytest.raises(OverflowError):
        await database.fetch_all("SELECT %s", [2**64])
    with pytest.raises(TypeError, match="object"):
        await database.fetch_all("SELECT %s", [object()])  # type: ignore[list-item]  # on purpose: not a parameter
    with pytest.raises(TypeError, match="list or tuple"):
        await database.fetch_all("SELECT %s", "a")


async def test_percent_signs_follow_djangos_rules(database: djox.Database) -> None:
    row = await database.fetch_one("SELECT '100%%', %s", ["ok"])
    assert row == ("100%", "ok")
