"""A small Django app for the tests: one model with a field of every common kind.

It checks that async queries give back exactly the values Django's sync
queries do, on every database (`tests/e2e/test_values.py`). It has no
migrations: Django makes its table from the model.
"""

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
