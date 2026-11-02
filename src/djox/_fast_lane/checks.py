"""Django checks each new kind of step once, before the fast lane takes it.

When a program calls `annotate(n=Count("chaptrs"))`, Django raises a
`FieldError` right away: it resolves every name when the method is called.
The fast lane only writes the call down, so it can't tell a wrong name from
a right one by itself. Instead, the first time a kind of step (its shape)
follows a kind of recipe (its shape), Django builds the query, with its own
`QuerySet` methods. If Django refuses, its error goes to the program, at the
same moment as without djox. If not, the pair is remembered: the next call
with the same shapes, with any values, is fine too.

Django's query also says two things the fast lane needs later, and they
are kept with the check:

- the output field of each annotation (`IntegerField` for `Count()`), to
  prepare the values of filters on it (`filter(n__gt=2)`), and
- whether the query has a `GROUP BY` (then the model's default ordering
  doesn't apply, which `first()` and `last()` must know).

Plain filters (`filter(pages__gt=500)`), slices and `reverse()` need no check:
`arguments.py` prepares filter values with their field, which raises the
same errors as Django.
"""

from dataclasses import dataclass
from typing import Any

from django.core.signals import setting_changed
from django.db import models

from djox._fast_lane.recipe import QueryRecipe, Step


@dataclass(frozen=True)
class Found:
    """What Django's query says after a checked step (see the module docs)."""

    annotation_fields: dict[str, "models.Field[Any, Any] | None"]
    groups_rows: bool


# What each check found, by model, recipe shape and step shape. Cleared when
# full, like the statements (`statements.py`).
checked: dict[tuple[Any, ...], Found] = {}
MAX_CHECKED = 2000


def recipe_with(model: type[models.Model], recipe: QueryRecipe, step: Step) -> QueryRecipe:
    """`recipe` with `step` added, once Django has checked that kind of step.

    Raises Django's error if Django refuses the step.
    """
    key = (model, recipe.shape, step.shape)
    found = checked.get(key)
    if found is None:
        found = check(model, recipe, step)
        if len(checked) >= MAX_CHECKED:
            checked.clear()
        checked[key] = found
    return recipe.with_step(step, annotation_fields=found.annotation_fields, groups_rows=found.groups_rows)


def check(model: type[models.Model], recipe: QueryRecipe, step: Step) -> Found:
    """Let Django build the query of `recipe` and `step`, and read what the fast lane needs from it."""
    queryset = step.apply_to(recipe.django_queryset(model))
    query = queryset.query
    annotation_fields = {}
    for name, annotation in query.annotations.items():
        annotation_fields[name] = output_field_of(annotation)
    return Found(annotation_fields, query.group_by is not None)


def output_field_of(annotation: Any) -> "models.Field[Any, Any] | None":
    """The output field of a resolved annotation, or `None` if Django can't
    tell yet.

    Django works it out only when it writes the SQL, and may fail then:
    `F("pages") + F("title")` mixes types (a `FieldError`), and an
    `OuterRef()` only has one inside a subquery. Here, any failure only
    means the fast lane doesn't know the field: Django's own error, if any,
    comes when the query runs, as without djox.
    """
    try:
        field: models.Field[Any, Any] = annotation.output_field
    except Exception:
        return None
    return field


def forget_checks(**_details: object) -> None:
    """Forget every check when a test changes Django's settings (`INSTALLED_APPS`...)."""
    checked.clear()


setting_changed.connect(forget_checks)
