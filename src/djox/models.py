"""`djox.Model`: a Django model whose async methods run on djox.

```python
import djox
from django.db import models


class Book(djox.Model):
    title = models.CharField(max_length=200)


book = await Book.objects.aget(title="Dune")  # `objects` is a `djox.Manager`
book.title = "Dune Messiah"
await book.asave()  # on one of djox's threads, not Django's sync thread
```

A model's own async methods (`asave()`, `adelete()`, `arefresh_from_db()`)
don't go through its manager, so `djox.Manager` alone can't change them.
`djox.Model` is an abstract model (no table of its own) that gives them, and
`objects = djox.Manager()`. Inherit from it instead of `models.Model`.
"""

from collections.abc import Iterable
from typing import Self

from django.db import models, router

from djox._threads import run_django_method
from djox.manager import Manager


class Model(models.Model):
    """`models.Model`, with `asave()`, `adelete()` and `arefresh_from_db()`
    running Django's sync methods on djox's own threads (see `djox._threads`).

    Inside a transaction (`djox.atomic()`, or Django's `TestCase`) they run
    on the transaction's thread, so they join it.
    """

    objects = Manager()

    class Meta:
        # `abstract`: Django makes no table for this class, only for its subclasses.
        abstract = True

    async def asave(
        self,
        *,
        force_insert: bool | tuple[models.base.ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        """Django's `save()`, on one of djox's threads."""
        alias = using or router.db_for_write(type(self), instance=self)

        def save() -> None:
            self.save(force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields)

        await run_django_method(alias, save, writes=True)

    async def adelete(self, using: str | None = None, keep_parents: bool = False) -> tuple[int, dict[str, int]]:
        """Django's `delete()`, on one of djox's threads."""
        alias = using or router.db_for_write(type(self), instance=self)
        return await run_django_method(alias, lambda: self.delete(using, keep_parents), writes=True)

    async def arefresh_from_db(
        self,
        using: str | None = None,
        fields: Iterable[str] | None = None,
        from_queryset: "models.QuerySet[Self] | None" = None,
    ) -> None:
        """Django's `refresh_from_db()`, on one of djox's threads."""
        alias = using or self._state.db or router.db_for_read(type(self), instance=self)
        await run_django_method(alias, lambda: self.refresh_from_db(using, fields, from_queryset), writes=False)
