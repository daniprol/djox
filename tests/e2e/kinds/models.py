"""A small Django app for the tests: a model with a field of every common
kind, and models whose saving is unusual.

`EveryKind` checks that async queries give back exactly the values Django's
sync queries do, on every database (`tests/e2e/test_values.py`). `Ticket`,
`Tag` and `Shout` check saving new objects (`tests/e2e/test_fast_lane_inserts.py`).
The app has no migrations: Django makes its tables from the models.
"""

import uuid
from typing import Any

from django.db import models

import djox


class EveryKind(models.Model):
    """One field of each kind Django stores differently. All may be empty (`NULL`)."""

    boolean = models.BooleanField(null=True)
    integer = models.IntegerField(null=True)
    big_integer = models.BigIntegerField(null=True)
    floating = models.FloatField(null=True)
    decimal = models.DecimalField(max_digits=12, decimal_places=4, null=True)
    char = models.CharField(max_length=50, null=True)
    text = models.TextField(null=True)
    date = models.DateField(null=True)
    time = models.TimeField(null=True)
    moment = models.DateTimeField(null=True)
    duration = models.DurationField(null=True)
    uuid = models.UUIDField(null=True)
    binary = models.BinaryField(null=True)
    json = models.JSONField(null=True)
    ip_address = models.GenericIPAddressField(null=True)

    objects = djox.Manager()


class Ticket(models.Model):
    """Values Django fills when it saves: a `UUID` primary key with a default,
    dates that `auto_now` and `auto_now_add` set, and a value the database
    gives when there is none (`db_default`)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    title = models.CharField(max_length=100)
    opened = models.DateTimeField(auto_now_add=True)
    changed = models.DateTimeField(auto_now=True)
    day = models.DateField(auto_now_add=True)
    priority = models.IntegerField(db_default=3)

    objects = djox.Manager()


class Tag(models.Model):
    """A primary key given by the caller, and nothing for the database to
    fill: Django's `INSERT` has no `RETURNING`, so there are no rows to read."""

    name = models.CharField(max_length=50, primary_key=True)
    color = models.CharField(max_length=20)

    objects = djox.Manager()


class Shout(models.Model):
    """A model with its own `save()`, which every way of saving must call."""

    text = models.CharField(max_length=100)

    objects = djox.Manager()

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.text = self.text.upper()
        super().save(*args, **kwargs)
