"""Plain Django models, written as if djox didn't exist.

`tests/settings.py` lists this app in `DJOX_MODELS`, so djox switches them
over when Django starts (`djox.install`).
"""

from django.db import models


class ShelfQuerySet(models.QuerySet["Shelf"]):
    """A custom `QuerySet`: its own methods must keep working."""

    def wide(self) -> "ShelfQuerySet":
        return self.filter(width__gte=100)


class ShelfManager(models.Manager.from_queryset(ShelfQuerySet)):  # type: ignore[misc]  # a class made by a call
    """A custom manager, kept in migrations like some libraries' managers."""

    use_in_migrations = True


class Label(models.Model):
    text = models.CharField(max_length=50)


class Shelf(models.Model):
    name = models.CharField(max_length=50)
    width = models.IntegerField(default=80)
    labels = models.ManyToManyField(Label, related_name="shelves")

    objects = ShelfManager()


class Box(models.Model):
    shelf = models.ForeignKey(Shelf, on_delete=models.CASCADE, related_name="boxes")
    contents = models.CharField(max_length=50)
