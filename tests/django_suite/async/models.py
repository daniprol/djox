from django.db import models
from django.utils import timezone

import djox


class RelatedModel(models.Model):
    simple = models.ForeignKey("SimpleModel", models.CASCADE, null=True)

    objects = djox.Manager()  # djox: Django's models use the default manager.


class SimpleModel(models.Model):
    field = models.IntegerField()
    created = models.DateTimeField(default=timezone.now)

    objects = djox.Manager()  # djox: Django's models use the default manager.


class ManyToManyModel(models.Model):
    simples = models.ManyToManyField("SimpleModel")

    objects = djox.Manager()  # djox: Django's models use the default manager.
