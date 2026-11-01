"""Async queries give back exactly the values sync queries do, for every
kind of field, on every database.

On Postgres this compares djox's Rust pool with psycopg, which Django's
sync code uses: Django's Postgres backend has no converters, so each value
must already be the right Python object (`Decimal`, `datetime`, `UUID`...).
On SQLite it compares djox's async database with Django's sync connection.

The tests run outside a transaction (`transaction=True`), so the async
queries really run on djox's async database.
"""

import datetime
import decimal
import uuid
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.db import connection, connections

import djox
from tests.e2e.kinds.models import EveryKind

pytestmark = pytest.mark.django_db(transaction=True)

VALUES: dict[str, Any] = {
    "boolean": True,
    "integer": -7,
    "big_integer": 2**40,
    "floating": 0.1,
    "decimal": decimal.Decimal("12.3400"),
    "char": "Dune",
    "text": "ünïcode ✓",
    "date": datetime.date(2024, 5, 1),
    "time": datetime.time(9, 30, 0, 250000),
    "moment": datetime.datetime(2024, 5, 1, 9, 30, 15, 123456, tzinfo=datetime.UTC),
    "duration": datetime.timedelta(days=2, seconds=3, microseconds=5),
    "uuid": uuid.UUID("a8098c1a-f86e-11da-bd1a-00112444be1e"),
    "binary": b"\x00\xff",
    "json": {"a": [1, 2.5, None], "b": "c"},
    "ip_address": "192.0.2.1",
}


def assert_same(first: object, second: object, field: str) -> None:
    """Equal, and of the same type (`True == 1`, but they aren't the same value)."""
    assert type(first) is type(second), f"{field}: {first!r} and {second!r}"
    assert first == second, field
    if isinstance(first, datetime.datetime) and isinstance(second, datetime.datetime):
        assert first.tzinfo == second.tzinfo, field


async def test_instances_are_the_same_from_async_and_sync_queries() -> None:
    saved = await sync_to_async(EveryKind.objects.create)(**VALUES)
    from_sync = await sync_to_async(EveryKind.objects.get)(pk=saved.pk)
    from_async = await EveryKind.objects.aget(pk=saved.pk)
    for field in VALUES:
        assert_same(getattr(from_async, field), getattr(from_sync, field), field)
    assert_same(from_async.decimal, VALUES["decimal"], "decimal")
    assert_same(from_async.moment, VALUES["moment"], "moment")


async def test_empty_fields_are_none_from_async_queries() -> None:
    saved = await EveryKind.objects.acreate()
    from_async = await EveryKind.objects.aget(pk=saved.pk)
    for field in VALUES:
        assert getattr(from_async, field) is None, field


async def test_values_and_values_list_are_the_same_from_async_and_sync_queries() -> None:
    await EveryKind.objects.acreate(**VALUES)
    fields = list(VALUES)
    from_sync = await sync_to_async(lambda: list(EveryKind.objects.values(*fields)))()
    from_async = [row async for row in EveryKind.objects.values(*fields)]
    for field in fields:
        assert_same(from_async[0][field], from_sync[0][field], field)

    from_sync_tuples = await sync_to_async(lambda: list(EveryKind.objects.values_list(*fields)))()
    from_async_tuples = [row async for row in EveryKind.objects.values_list(*fields)]
    for index, field in enumerate(fields):
        assert_same(from_async_tuples[0][index], from_sync_tuples[0][index], field)


async def test_filters_send_each_kind_of_value() -> None:
    saved = await EveryKind.objects.acreate(**VALUES)
    for field, value in VALUES.items():
        if field in ("binary", "json"):
            continue  # Django can't compare those with `=` on every database
        assert await EveryKind.objects.filter(**{field: value}).aexists(), field
    assert await EveryKind.objects.filter(moment__gte=VALUES["moment"]).acount() == 1
    assert (await EveryKind.objects.filter(json__a__1=2.5).aget()).pk == saved.pk


async def test_raw_rows_are_the_same_from_djox_and_djangos_cursor() -> None:
    await EveryKind.objects.acreate(**VALUES)
    columns = ", ".join(field for field in VALUES if field != "binary")
    sql = f"SELECT {columns} FROM kinds_everykind"

    def fetch_with_django() -> list[Any]:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            rows: list[Any] = cursor.fetchall()
            return rows

    from_django = await sync_to_async(fetch_with_django)()
    from_djox = await djox.connections["default"].fetch_all(sql)
    for index, field in enumerate(field for field in VALUES if field != "binary"):
        assert_same(from_djox[0][index], from_django[0][index], field)


async def test_moments_follow_the_time_zone_settings(settings: Any) -> None:
    # Without time zone support, Django stores and reads local times in TIME_ZONE.
    settings.USE_TZ = False
    settings.TIME_ZONE = "America/New_York"
    # Django keeps a connection's time zone per thread, and updates only
    # this thread's when the settings change: start the sync thread afresh.
    await sync_to_async(forget_this_threads_connection)()
    try:
        moment = datetime.datetime(2024, 5, 1, 9, 30)
        saved = await sync_to_async(EveryKind.objects.create)(moment=moment)
        from_async = await EveryKind.objects.aget(pk=saved.pk)
        assert_same(from_async.moment, moment, "moment")
        from_sync = await sync_to_async(EveryKind.objects.get)(pk=saved.pk)
        assert_same(from_async.moment, from_sync.moment, "moment")
        assert await EveryKind.objects.filter(moment=moment).aexists()
    finally:
        await sync_to_async(forget_this_threads_connection)()


def forget_this_threads_connection() -> None:
    """Close this thread's Django connection and forget it, so the next one
    reads the settings again."""
    connections["default"].close()
    del connections["default"]
