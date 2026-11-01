"""`djox.Manager`: a Django model manager whose `QuerySet`s are `djox.QuerySet`s.

```python
import djox
from django.db import models


class Book(models.Model):
    title = models.CharField(max_length=200)

    objects = djox.Manager()


book = await Book.objects.aget(title="Dune")  # runs on djox, no thread waits
```
"""

from typing import TypeVar

from django.db import models

from djox.query import QuerySet
from djox.related import RunRelatedManagersOnDjox

ModelType = TypeVar("ModelType", bound=models.Model)


class Manager(RunRelatedManagersOnDjox, models.Manager[ModelType]):
    """Django's `Manager`, handing out `djox.QuerySet`s.

    A manager's methods (`Book.objects.filter()`, `Book.objects.aget()`...)
    all start from `get_queryset()`, so that is the main change. The mixin
    sends related managers' async methods (`author.books.aadd()`...) to
    djox's threads (see `djox.related`).
    """

    def get_queryset(self) -> QuerySet[ModelType]:
        """A new `djox.QuerySet` of all the model's objects, like Django's `get_queryset()`."""
        return QuerySet(model=self.model, using=self._db, hints=self._hints)  # type: ignore[attr-defined]  # Django's, not in django-stubs
