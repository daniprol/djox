"""Understanding `filter()`, `exclude()`, `order_by()` and `update()` arguments, the way Django would.

`condition_for(Book, "pages__gt", 500)` finds the field (`pages`) and the
lookup (`gt`), and prepares the value with the field's own
`get_prep_value()`, which is what Django does when `filter()` runs. So a
wrong value raises the same error, at the same moment, as in Django.
`where_for()` does the same for a whole `filter()` or `exclude()` call,
`Q` objects included (`Q(pages__gt=500) | ~Q(in_print=True)`).

A name may follow foreign keys and one-to-one fields forward, to any depth
(`author__name`, `edition__book__author__name__in`): each points to one row,
so Django joins its table and the query stays one plain `SELECT`.

Anything this file doesn't fully understand returns `None`: the `QuerySet`
then hands the call to Django right away. That covers relations that point
to many rows (`books__title`, a many-to-many field), transforms
(`published__year`), other lookups (`icontains`), expressions
(`F("pages")`, `Exists(...)`), `XOR`, and values of a type the field
doesn't usually get (`filter(pages=7.5)`).

The SQL itself always comes from Django (`djox._fast_lane.statements`). This
file only has to say which queries share their SQL, and compute the
parameters Django would compute.
"""

import datetime
import decimal
import uuid
from functools import lru_cache
from typing import Any

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import Q, lookups
from django.db.models.constants import LOOKUP_SEP
from django.db.models.fields import related_lookups

from djox._fast_lane.simple_query import Condition, Update, Where

# The lookup classes the fast lane knows, by lookup name: Django's plain ones,
# the integer ones (which refuse numbers too big for the column) and the ones
# for foreign keys (which accept a model instance). A field that registers its
# own lookup class (`register_lookup()`) gets Django's way.
KNOWN_LOOKUP_CLASSES: "dict[str, set[type[lookups.Lookup[Any]]]]" = {
    "exact": {lookups.Exact, lookups.IntegerFieldExact, related_lookups.RelatedExact},
    "gt": {lookups.GreaterThan, lookups.IntegerGreaterThan, related_lookups.RelatedGreaterThan},
    "gte": {lookups.GreaterThanOrEqual, lookups.IntegerGreaterThanOrEqual, related_lookups.RelatedGreaterThanOrEqual},
    "lt": {lookups.LessThan, lookups.IntegerLessThan, related_lookups.RelatedLessThan},
    "lte": {lookups.LessThanOrEqual, lookups.IntegerLessThanOrEqual, related_lookups.RelatedLessThanOrEqual},
    "in": {lookups.In, related_lookups.RelatedIn},
    "isnull": {lookups.IsNull, related_lookups.RelatedIsNull},
}

INTEGER_TYPES = {
    "AutoField",
    "BigAutoField",
    "SmallAutoField",
    "IntegerField",
    "BigIntegerField",
    "SmallIntegerField",
    "PositiveIntegerField",
    "PositiveBigIntegerField",
    "PositiveSmallIntegerField",
}

# The Python types a filter value may have, by the field's internal type. A
# value of another type (a float for an integer field, a string for a date...)
# is still allowed by Django, which converts it in its own ways: the fast lane
# lets Django do that.
#
# Text is allowed for integers and UUIDs, because ids often come from URLs
# (`/books/5/` gives `pk="5"`, which is what Django REST framework's views
# pass). The field's own `get_prep_value()` turns it into a number or a
# `uuid.UUID`, exactly as in Django, so `"5"` gives the same query as `5`, and
# `"five"` raises the same error, at the same moment.
VALUE_TYPES: dict[str, tuple[type, ...]] = {
    **dict.fromkeys(INTEGER_TYPES, (int, str)),
    "CharField": (str,),
    "TextField": (str,),
    "SlugField": (str,),
    "EmailField": (str,),
    "URLField": (str,),
    "BooleanField": (bool,),
    "FloatField": (float, int),
    "DecimalField": (decimal.Decimal, int),
    "DateTimeField": (datetime.datetime,),
    "DateField": (datetime.date,),
    "TimeField": (datetime.time,),
    "UUIDField": (uuid.UUID, str),
}

# Returned by `prepare()` for a value the fast lane leaves to Django.
NOT_SIMPLE = object()


def where_for(model: type[models.Model], args: tuple[Any, ...], kwargs: dict[str, Any], negated: bool) -> Where | None:
    """The fast lane's note of one `filter(*args, **kwargs)` call (or
    `exclude()`, `negated`), or `None` if Django should handle it.

    Django turns the call into `Q(*args, **kwargs)`, whose children are the
    `Q` objects first, then the keyword arguments sorted by name; the note
    keeps them in that order, which is the order of the SQL's parameters.
    """
    children = [*args, *sorted(kwargs.items())]
    return node_for(model, children, Q.AND, negated)


def node_for(model: type[models.Model], children: list[Any], connector: str, negated: bool) -> Where | None:
    """The `Where` for the children of a `Q` (or of a `filter()` call), or `None`."""
    if connector not in (Q.AND, Q.OR) or not children:
        return None  # `XOR` has SQL of its own on some databases; an empty `Q()` adds nothing
    noted: list[Condition | Where] = []
    for child in children:
        node: Condition | Where | None = None
        # A `Q` exactly: a subclass may build its filters its own way.
        if type(child) is Q:
            node = node_for(model, child.children, child.connector, child.negated)
        elif type(child) is tuple and len(child) == 2 and isinstance(child[0], str):
            node = condition_for(model, child[0], child[1])
        # Anything else (an expression like `Exists(...)`) leaves `node` at `None`.
        if node is None:
            return None
        noted.append(node)
    return Where(tuple(noted), connector, negated)


def condition_for(model: type[models.Model], name: str, value: Any) -> Condition | None:
    """The `Condition` for one `filter()` argument, like `pages__gt=500`, or
    `None` if Django should handle it."""
    found = field_and_lookup(model, name)
    if found is None:
        return None
    field, lookup, lookup_class = found
    # A copy of a list or set (`title__in=titles`): Django reads the values
    # when `filter()` runs, so changing `titles` afterwards changes nothing.
    given = type(value)(value) if type(value) in (list, set) else value
    if lookup == "isnull":
        if type(value) is not bool:  # Django raises for anything else
            return None
        return Condition(name, given, field, "isnull", (), detail=value)
    if value is None:
        # Django reads `exact=None` as `isnull=True`, and refuses `None` for the other lookups.
        return Condition(name, given, field, "isnull", (), detail=True) if lookup == "exact" else None
    if lookup == "in":
        prepared_values = prepare_in_values(field, value)
        if prepared_values is None:
            return None
        # Under `exclude()`, Django writes `OR "column" IS NULL` when the values include `None`.
        includes_none = any(item is None for item in value)
        return Condition(name, given, field, "in", prepared_values, detail=(len(prepared_values), includes_none))
    prepared = prepare(field, value)
    if prepared is NOT_SIMPLE:
        return None
    return Condition(
        name,
        given,
        field,
        lookup,
        (prepared,),
        detail=prepared if isinstance(prepared, bool) else None,
        checks_integer_range=issubclass(lookup_class, lookups.IntegerFieldOverflow),
    )


def prepare_in_values(field: "models.Field[Any, Any]", values: Any) -> tuple[Any, ...] | None:
    """The values of `field__in=[...]`, prepared, or `None`. Like Django, the
    values are prepared one by one, then `None` and repeated values are dropped."""
    if type(values) not in (list, tuple, set, frozenset):
        return None
    prepared_values = []
    for value in values:
        if value is None:
            continue
        prepared = prepare(field, value)
        if prepared is NOT_SIMPLE:
            return None
        prepared_values.append(prepared)
    try:
        # `dict.fromkeys()` drops repeated values and keeps the order, like Django's `OrderedSet`.
        return tuple(dict.fromkeys(prepared_values))
    except TypeError:  # a value that can't be hashed: Django keeps them all too
        return tuple(prepared_values)


def prepare(field: "models.Field[Any, Any]", value: Any) -> Any:
    """`value` prepared by `field`, like Django's lookup prepares it, or
    `NOT_SIMPLE` if Django should handle it."""
    if hasattr(value, "resolve_expression"):  # F(), Value(), a subquery, a QuerySet...
        return NOT_SIMPLE
    value_field = field
    if field.is_relation:
        # `author=some_author` means `author_id=some_author.pk` (Django's `get_normalized_value()`).
        value_field = field.target_field  # type: ignore[attr-defined]  # foreign keys have it
        if isinstance(value, models.Model):
            related_model = field.related_model
            if type(value)._meta.concrete_model is not related_model._meta.concrete_model:  # type: ignore[union-attr]
                return NOT_SIMPLE  # Django raises "Cannot query ...: Must be ... instance."
            value = getattr(value, value_field.attname)
            if value is None:
                return NOT_SIMPLE  # Django raises: the instance isn't saved
    elif isinstance(value, models.Model):
        return NOT_SIMPLE
    allowed_types = VALUE_TYPES.get(value_field.get_internal_type())
    if allowed_types is None or type(value) not in allowed_types:
        return NOT_SIMPLE
    return field.get_prep_value(value)


@lru_cache(maxsize=4096)
def field_and_lookup(
    model: type[models.Model], name: str
) -> "tuple[models.Field[Any, Any], str, type[lookups.Lookup[Any]]] | None":
    """The field, the lookup name and Django's lookup class for a `filter()`
    name like `"pages__gt"`, `"pk"`, `"author"` or `"author__name__in"`, or `None`.

    Cached per model and name: models don't change while the program runs.
    (`@lru_cache` remembers what a function returned for each argument.)
    """
    parts = name.split(LOOKUP_SEP)
    found = field_at_path(model, parts)
    if found is None:
        return None
    field, lookup_parts = found
    if len(lookup_parts) > 1:
        return None  # a transform, like `published__year__gt`
    lookup = lookup_parts[0] if lookup_parts else "exact"
    if lookup not in KNOWN_LOOKUP_CLASSES:
        return None
    lookup_class = field.get_lookup(lookup)
    if lookup_class not in KNOWN_LOOKUP_CLASSES[lookup]:
        return None
    return field, lookup, lookup_class


def field_at_path(model: type[models.Model], parts: list[str]) -> "tuple[models.Field[Any, Any], list[str]] | None":
    """The field the start of `parts` names, following relations, and the
    parts left after it (the lookup). `None` if a part isn't a field
    `model_field()` accepts.

    `["author", "name", "in"]` on `Book` gives `Author.name` and `["in"]`.
    Like Django, a part is a field name if the model has such a field, and
    a lookup otherwise: only a relation (a foreign key or a one-to-one
    field) leads to another model's fields.
    """
    field = model_field(model, parts[0])
    if field is None:
        return None
    position = 1
    while position < len(parts) and field.is_relation:
        # `related_model` is the model the relation points to (`Author` for `Book.author`).
        next_field = model_field(field.related_model, parts[position])  # type: ignore[arg-type]  # a relation has one
        if next_field is None:
            break  # not a field of the related model: a lookup on the relation, like `author__in`
        field = next_field
        position += 1
    return field, parts[position:]


def model_field(model: type[models.Model], name: str) -> "models.Field[Any, Any] | None":
    """The field `name` (or `"pk"`) stands for, if it is one column of the model's own table.

    That is a plain field or a foreign key (`author`, or its column `author_id`),
    but not a many-to-many field, a reverse relation (`author.books`) or a
    primary key made of several columns.
    """
    meta = model._meta
    try:
        field = meta.pk if name == "pk" else meta.get_field(name)
    except FieldDoesNotExist:
        return None
    if not isinstance(field, models.Field) or not field.concrete or field.column is None:
        return None
    if field.many_to_many:
        return None
    if field.is_relation and len(field.foreign_related_fields) != 1:  # type: ignore[attr-defined]  # foreign keys have it
        return None
    return field


def is_simple_ordering(model: type[models.Model], names: tuple[Any, ...]) -> bool:
    """Whether every `order_by()` argument names a column, like `"-pages"`,
    `"pk"`, or one reached through relations (`"author__name"`), the way
    `filter()` names them. Expressions, `"?"` (random) and transforms are
    left to Django."""
    for name in names:
        if not isinstance(name, str):
            return False
        found = field_at_path(model, name.removeprefix("-").split(LOOKUP_SEP))
        if found is None or found[1]:
            return False  # not a field, or something after it (a transform)
    return True


def updates_for(model: type[models.Model], keyword_arguments: dict[str, Any]) -> tuple[Update, ...] | None:
    """The values of one `update()` call, in the order Django writes them
    (the order given), or `None` if one of them must go to Django."""
    if not keyword_arguments:
        return None  # Django runs no SQL for `update()` with nothing to set
    updates = []
    for name, value in keyword_arguments.items():
        field = updated_field(model, name)
        if field is None:
            return None
        if isinstance(value, models.Model):
            return None  # Django sets its primary key: its own way
        if value is not None and prepare(field, value) is NOT_SIMPLE:
            return None  # an expression (`F("pages") + 1`), a value of an unusual type...
        updates.append(Update(name, field, value))
    return tuple(updates)


def updated_field(model: type[models.Model], name: str) -> "models.Field[Any, Any] | None":
    """The field `update(name=...)` sets, if it is one plain column of the
    model's own table. Django's `UPDATE` compiler writes those as `"column" = %s`."""
    if name == "pk":
        return None  # Django refuses `pk` in some cases: let it decide
    field = model_field(model, name)
    if field is None or field.generated:
        return None  # Django skips generated fields
    if field.model._meta.concrete_model is not model._meta.concrete_model:
        return None  # a parent model's field: another table
    if hasattr(field, "get_placeholder"):
        return None  # the field writes its own SQL for its values
    return field
