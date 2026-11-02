# Choose the models

Three ways to put a model on djox. Most projects only need the first.

| Way | For |
|---|---|
| [`DJOX_MODELS`](#djox_models) | Every model, or the ones you list, `User` and other libraries' models included. No code changes |
| [`djox.Manager()`](#djoxmanager) | One model whose code you own |
| [`djox.Model`](#djoxmodel) | One model whose code you own, its `asave()` and `adelete()` included |

## `DJOX_MODELS`

```python
# settings.py
INSTALLED_APPS = [..., "djox"]
DJOX_MODELS = "__all__"  # the default: every model
# DJOX_MODELS = ["auth", "shop.Order"]  # or some apps and models
# DJOX_MODELS = []  # or none
```

When Django starts, djox switches each chosen model:

- **Its managers** (`objects` and any custom one) return `djox.QuerySet`s. A
  custom `QuerySet` keeps its own methods (`Shelf.objects.wide()` still
  works).
- **Its related managers** (`user.groups.aadd(...)`) run on djox's threads.
- **Its `asave()`, `adelete()` and `arefresh_from_db()`** become
  `djox.Model`'s, unless the model defines its own.

What doesn't change:

- The model's code, tables and migrations. A switched manager keeps its class
  name, so `makemigrations` and type checkers see the model as before.
- A model that already uses `djox.Manager` or `djox.Model`.
- Models on a database whose `ENGINE` isn't djox's: they keep Django's
  behavior.

## `djox.Manager`

```python
import djox
from django.db import models


class Book(models.Model):
    title = models.CharField(max_length=200)
    pages = models.IntegerField()

    objects = djox.Manager()
```

- Works without `"djox"` in `INSTALLED_APPS`.
- A custom `QuerySet` can subclass `djox.QuerySet`, and use
  `MyQuerySet.as_manager()`.

## `djox.Model`

A model's own `asave()`, `adelete()` and `arefresh_from_db()` don't go
through its manager. Inherit from `djox.Model` to run them on djox's threads
too:

```python
class Author(djox.Model):
    name = models.CharField(max_length=100)


author = Author(name="Frank Herbert")
await author.asave()
```

- It also sets `objects = djox.Manager()`.
- It is abstract (no table), so it changes no migration.
