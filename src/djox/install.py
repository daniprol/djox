"""Switch existing models to djox, without changing their code.

`djox.Model` and `djox.Manager` work for models you write. For the rest (a
project's existing models, `django.contrib.auth`'s `User`, other libraries'
models), add djox as an app:

```python
INSTALLED_APPS = [..., "djox"]
DJOX_MODELS = "__all__"  # the default; or ["auth", "shop.Order"], or []
```

When Django is ready, djox switches each chosen model over (`use_djox()`):

1. **Its managers** become djox managers: each manager's class gets a
   subclass made here, whose `QuerySet`s are `djox.QuerySet`s and whose
   related managers (`author.books`, `user.groups`) run on djox's threads.
   A custom `QuerySet` keeps its own methods; djox only fills in the async
   methods it didn't write itself.
2. **Its `asave()`, `adelete()` and `arefresh_from_db()`** become
   `djox.Model`'s, unless the model wrote its own.

Nothing changes in the database or the migrations, and a model on a database
whose `ENGINE` isn't djox's keeps Django's behaviour (every djox method falls
back to Django's there).

`DJOX_MODELS` lists app labels (`"auth"`: every model of the app) and model
labels (`"shop.Order"`). `"__all__"` means every model.

The `QuerySet` classes made here live in this module under plain names, so
a pickled `QuerySet` (a cached query, say) can find its class again.
"""

import sys
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models.options import Options
from django.utils.functional import cached_property

from djox.models import Model
from djox.query import QuerySet
from djox.related import RunRelatedManagersOnDjox

# `djox.Model`'s methods, put on chosen models that don't define their own.
MODEL_METHODS = ("asave", "adelete", "arefresh_from_db")


def use_djox_for_chosen_models() -> None:
    """Switch every model `DJOX_MODELS` chooses (called when Django is ready)."""
    chosen = getattr(settings, "DJOX_MODELS", "__all__")
    for model in apps.get_models():
        if is_chosen(model, chosen):
            use_djox(model)


def is_chosen(model: type[models.Model], chosen: object) -> bool:
    """Whether `DJOX_MODELS` (`chosen`) names `model` or its app."""
    if chosen == "__all__":
        return True
    if not isinstance(chosen, list | tuple):
        raise ImproperlyConfigured(
            "DJOX_MODELS must be '__all__' or a list of app labels and model labels, like ['auth', 'shop.Order']."
        )
    return model._meta.app_label in chosen or model._meta.label in chosen


def use_djox(model: type[models.Model]) -> None:
    """Switch one model's managers and async model methods to djox (see the module docs).

    Calling it again on the same model changes nothing.
    """
    if model._meta.abstract or model._meta.swapped:
        return  # no table of its own, or replaced by another model (like a custom `User`)
    if not isinstance(model._meta, OptionsOnDjox):
        # Giving an object a new class keeps its attributes, like setting
        # `obj.__class__` in any Python: this model's `_meta` stays the same
        # object, with one property computed differently.
        model._meta.__class__ = OptionsOnDjox
        for cached in ("managers", "managers_map", "base_manager", "default_manager"):
            model._meta.__dict__.pop(cached, None)  # computed again, the djox way, when next needed
    if not issubclass(model, Model):
        for name in MODEL_METHODS:
            if getattr(model, name) is getattr(models.Model, name):  # the model didn't write its own
                setattr(model, name, getattr(Model, name))


class OptionsOnDjox(Options):  # type: ignore[type-arg]  # generic only in django-stubs, not at run time
    """A chosen model's `_meta` (Django's `Options`), whose managers are djox's.

    Django computes `_meta.managers` as copies of the managers the model
    declares or inherits, one set per model, and computes them again
    whenever its caches are cleared (tests do that). So the switch happens
    there: each time, this model's own copies get a djox class.
    """

    @cached_property
    def managers(self) -> Any:
        """Django's `managers`, with each copy's class switched to djox (`manager_class_on_djox()`)."""
        managers = Options.managers.func(self)  # type: ignore[attr-defined]  # Django's own computation
        for manager in managers:
            if not isinstance(manager, RunRelatedManagersOnDjox):  # `djox.Manager` already is one
                manager.__class__ = manager_class_on_djox(type(manager))
        return managers


def manager_class_on_djox(manager_class: type[models.Manager[Any]]) -> type:
    """A subclass of `manager_class` whose `QuerySet`s and related managers use djox (made once).

    It presents itself as `manager_class`: the same name and module, and
    `==` answers like it. Django's migrations write managers down by name
    and compare them with `==`, and type checkers (django-stubs) look
    managers up by name, so to all of them the model keeps its own manager:
    adding or removing djox changes no migration.
    """
    made = made_manager_classes.get(manager_class)
    if made is None:
        queryset_class = queryset_class_on_djox(manager_class._queryset_class)  # type: ignore[attr-defined]  # Django's
        # `type(name, bases, attributes)` makes a class, like a `class` statement does.
        made = type(
            manager_class.__name__,
            (manager_class, RunRelatedManagersOnDjox),
            {
                "__module__": manager_class.__module__,
                "__qualname__": manager_class.__qualname__,
                "_queryset_class": queryset_class,
                "original_class": manager_class,
                "__eq__": equal_as_original,
                "__hash__": manager_class.__hash__,  # defining `__eq__` would remove it otherwise
            },
        )
        made_manager_classes[manager_class] = made
    return made


made_manager_classes: dict[type, type] = {}


def queryset_class_on_djox(queryset_class: type[models.QuerySet[Any]]) -> type:
    """A `QuerySet` class with `queryset_class`'s own methods first, then `djox.QuerySet`'s (made once)."""
    if issubclass(queryset_class, QuerySet):
        return queryset_class
    if queryset_class is models.QuerySet:
        return QuerySet
    name = class_name("QuerySet", queryset_class)
    made = getattr(sys.modules[__name__], name, None)
    if made is None:
        # The class looks methods up in `queryset_class` (the model's custom
        # `QuerySet`), then in `djox.QuerySet`, then in Django's `QuerySet`.
        made = type(name, (queryset_class, QuerySet), {"__module__": __name__})
        setattr(sys.modules[__name__], name, made)
    return made


def class_name(kind: str, original: type) -> str:
    """A plain, unique name for the class made from `original`: `OnDjox_<module>_<class>_<kind>`."""
    path = f"{original.__module__}.{original.__qualname__}".replace(".", "_").replace("<", "").replace(">", "")
    return f"OnDjox_{path}_{kind}"


def equal_as_original(self: Any, other: object) -> bool:
    """`==` as the original manager class answers it.

    Django's `Manager.__eq__` checks `isinstance(other, type(self))`, and
    migrations compare the managers they wrote down (of the original class)
    with the model's: without this, they would always differ.
    """
    return isinstance(other, self.original_class) and self._constructor_args == other._constructor_args
