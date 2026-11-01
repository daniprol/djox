"""Related managers (`author.books`, `pizza.toppings`) running on djox's threads.

`author.books.acreate(...)`, `pizza.toppings.aadd(topping)` and the like are
methods of a *related manager*: a class Django builds the first time it is
needed, as a subclass of the related model's default manager class. Django
gives it its own async methods, each running the sync method on Django's
single sync thread (`sync_to_async(self.add)`).

`RunRelatedManagersOnDjox` is a mixin for manager classes. When Django builds
a related manager from such a class, the mixin swaps those async methods for
ones that run the same sync method on djox's own threads (`djox._threads`):
in the transaction this code is in, or on a pooled thread, with the write
turn. Django's code still does the work (the foreign keys, the `through`
rows, the `m2m_changed` signals), so the results are exactly Django's.

`djox.Manager` has the mixin; so do the managers that `"djox"` in
`INSTALLED_APPS` switches over (`djox.install`).
"""

from collections.abc import Callable, Coroutine
from typing import Any

from django.db import router

from djox import _threads

# The async methods Django gives related managers, and the sync method each runs.
RELATED_MANAGER_METHODS = {
    "acreate": "create",
    "aget_or_create": "get_or_create",
    "aupdate_or_create": "update_or_create",
    "aadd": "add",
    "aremove": "remove",
    "aclear": "clear",
    "aset": "set",
}


class RunRelatedManagersOnDjox:
    """A mixin for manager classes: their related managers' async methods run on djox's threads."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Called by Python each time a subclass of this class is created
        (like a class decorator that applies to every subclass).

        Only the classes Django builds (in its own modules) are changed: a
        manager you write yourself keeps its own async methods.
        """
        super().__init_subclass__(**kwargs)
        if not cls.__module__.startswith("django."):
            return
        for async_name, sync_name in RELATED_MANAGER_METHODS.items():
            if async_name in cls.__dict__:  # defined by this class itself, not inherited
                setattr(cls, async_name, run_on_djox_threads(async_name, sync_name))


def run_on_djox_threads(async_name: str, sync_name: str) -> Callable[..., Coroutine[Any, Any, Any]]:
    """An async method that runs the related manager's sync method `sync_name` on djox's threads."""

    async def method(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Many-to-many managers write to their `through` table; the others to their model's.
        written_model = getattr(self, "through", None) or self.model
        alias = router.db_for_write(written_model, instance=self.instance)
        sync_method = getattr(self, sync_name)
        return await _threads.run_django_method(alias, lambda: sync_method(*args, **kwargs), writes=True)

    method.__name__ = method.__qualname__ = async_name
    # Django marks methods that change data, so templates never call them.
    method.alters_data = True  # type: ignore[attr-defined]  # a plain attribute on the function
    return method
