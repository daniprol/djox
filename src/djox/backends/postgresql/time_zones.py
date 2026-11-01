"""Moves the dates and times of async rows into the connection's time zone.

A Postgres `timestamptz` is a moment in time. psycopg, as Django sets it up,
hands it over in the connection's time zone (`DatabaseWrapper.timezone`),
without the zone when `USE_TZ = False`. djox's async database hands it over
in UTC. With Django's default settings (`USE_TZ = True`, no database
`TIME_ZONE`) the connection's zone is UTC, so the rows are already right and
this does nothing. For other settings, each moment is moved here.
"""

import datetime
import zoneinfo
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from djox._core import Row


def rows_in_time_zone(rows: "list[Row]", time_zone_name: str, keeps_zone: bool) -> "list[Row]":
    """`rows` with each aware UTC `datetime` moved to `time_zone_name`, and,
    when `keeps_zone` is false (`USE_TZ = False`), the zone taken off."""
    time_zone = zoneinfo.ZoneInfo(time_zone_name)
    moved_rows: list[Row] = []
    for row in rows:
        moved = []
        for value in row:
            if isinstance(value, datetime.datetime) and value.tzinfo is datetime.UTC:
                value = value.astimezone(time_zone)
                if not keeps_zone:
                    value = value.replace(tzinfo=None)
            moved.append(value)
        moved_rows.append(tuple(moved))
    return moved_rows


def needs_moving(time_zone_name: str, use_tz: bool) -> bool:
    """Whether rows from djox's async database must be moved (see the module docs)."""
    return not use_tz or time_zone_name != "UTC"
