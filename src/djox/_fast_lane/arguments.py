"""Reading the arguments of a `QuerySet` call into a `Step`: its shape and its values.

`filter(pages__gt=500)` and `filter(pages__gt=7)` make the same SQL with a
different parameter. To see that, the fast lane reads each call's arguments
and splits them into:

- the **shape**: everything that decides the SQL, like the name
  `"pages__gt"`, the expression `Count("chapters")`, `None`, `True`;
- the **values**: everything that only becomes a parameter, like `500`,
  each a `QueryValue` that knows how Django prepares it.

| Call                            | What is read                                         |
|---------------------------------|------------------------------------------------------|
| `filter()`, `exclude()`         | names, lookups, values prepared by their field (`filter_step()`) |
| `annotate()`, `order_by()`...   | names and expressions (`step_for()`, see `METHODS_WITH_EXPRESSIONS`) |
| `update()`                      | fields and the values they get (`update_step()`)     |
| `queryset[10:20]`, `reverse()`  | nothing but the shape (`slice_step()`, `reverse_step()`) |

**Filters** (`condition_for()`) find the field and the lookup of a name
like `"author__name__in"`, and prepare the value with the field's own
`get_prep_value()`, which is what Django does when `filter()` runs. So a
wrong value raises the same error, at the same moment, as in Django. A name
may follow any relation: foreign keys (`author__name`), reverse ones
(`chapters__title`), many-to-many fields (`tags__name`). It may also name an
annotation made before (`n__gt=2` after `annotate(n=Count(...))`).

**Expressions** (`shape_of()`) are only Django's own classes (`F`, `Value`,
`Count`, `Case`...): their SQL depends on their arguments and nothing else,
so the shape of `F("pages") + 1` is the class and the shape of each argument.
A number, date or text in a `Value()` is a value, prepared like Django's
`Value` prepares it (with the field Django picks for its type).

Anything this file doesn't fully understand returns `None`: the `QuerySet`
then hands the call to Django right away. That covers transforms
(`published__year`), lookups other than `exact`, `gt`, `gte`, `lt`, `lte`,
`in` and `isnull`, subqueries (`Exists()`, `Subquery()`), expression
classes of the program's own, `XOR`, and values of a type the field doesn't
usually get (`filter(pages=7.5)`).

The SQL itself always comes from Django (`statements.py`). This file only
has to say which queries share their SQL, and what their values are.
"""

import datetime
import decimal
import uuid
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import Q, lookups
from django.db.models.constants import LOOKUP_SEP
from django.db.models.expressions import When
from django.db.models.fields import related_lookups
from django.utils import timezone
from django.utils.hashable import make_hashable

from djox._fast_lane.recipe import Condition, QueryRecipe, QueryValue, Step, Where

# The lookup classes the fast lane knows, by lookup name: Django's plain ones,
# the integer ones (which refuse numbers too big for the column) and the ones
# for relations (which accept a model instance). A field that registers its
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

# The field Django's `Value()` picks for a value of each type (its
# `_resolve_output_field()`), which prepares the value. Text is not here: in
# an expression's arguments, text is a name (`Count("chapters")`); a text
# value must be given as `Value("text")`.
EXPRESSION_VALUE_FIELDS: "dict[type, models.Field[Any, Any]]" = {
    int: models.IntegerField(),
    float: models.FloatField(),
    decimal.Decimal: models.DecimalField(),
    datetime.datetime: models.DateTimeField(),
    datetime.date: models.DateField(),
    datetime.time: models.TimeField(),
    datetime.timedelta: models.DurationField(),
    uuid.UUID: models.UUIDField(),
}

# Returned for anything the fast lane leaves to Django. (`object()` is a
# value equal to nothing else, so it can't be confused with a real answer.)
NOT_SIMPLE: Any = object()

# The `QuerySet` methods whose arguments are names and expressions (see
# `step_for()`), and whether their positional arguments may be expressions
# too (`order_by(F("pages").desc())`) or only names (`values("title")`).
METHODS_WITH_EXPRESSIONS = {
    "annotate": True,
    "alias": True,
    "aggregate": True,
    "order_by": True,
    "values": False,
    "values_list": False,
    "distinct": False,
    "select_related": False,
}


# ---------------------------------------------------------------- steps


def filter_step(
    model: type[models.Model], recipe: QueryRecipe, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Step | None:
    """The step of one `filter(*args, **kwargs)` call (or `exclude()`), or
    `None` if Django should handle it.

    Django turns the call into `Q(*args, **kwargs)`, whose children are the
    `Q` objects first, then the keyword arguments sorted by name; the step
    keeps them in that order, which is the order of the SQL's parameters.
    """
    reader = ArgumentReader(model, recipe.annotation_fields)
    children = [*args, *sorted(kwargs.items())]
    where = reader.where_for(children, Q.AND, negated=(method == "exclude"))
    if where is None:
        return None
    return Step(
        method,
        shape=(method, where.shape),
        values=where.values,
        where=where,
        needs_check=reader.needs_check,
        uses_expressions=reader.uses_expressions,
    )


def step_for(
    model: type[models.Model], recipe: QueryRecipe, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Step | None:
    """The step of a call whose arguments are names and expressions, like
    `annotate(n=Count("chapters"))` or `order_by("-pages")` (see
    `METHODS_WITH_EXPRESSIONS`), or `None` if Django should handle it.

    Django checks this kind of step once before the fast lane takes it
    (`checks.py`): a wrong name raises Django's error, at Django's moment.
    """
    reader = ArgumentReader(model, recipe.annotation_fields)
    positional_shapes = []
    for argument in args:
        is_name = isinstance(argument, str) or argument is None  # `select_related(None)` is a name too
        if not is_name and not METHODS_WITH_EXPRESSIONS[method]:
            return None  # an expression where Django takes names: Django's way
        positional_shapes.append(reader.shape_of(argument))
    # The keyword arguments keep their order: it is the order of the columns.
    keyword_shapes = []
    for name, argument in kwargs.items():
        keyword_shapes.append((name, reader.shape_of(argument)))
    shape = (method, tuple(positional_shapes), tuple(keyword_shapes))
    if reader.found_something_not_simple:
        return None
    return Step(
        method,
        args,
        dict(kwargs),
        shape,
        tuple(reader.values),
        needs_check=True,
        uses_expressions=reader.uses_expressions,
    )


def update_step(model: type[models.Model], kwargs: dict[str, Any]) -> Step | None:
    """The values one `update(**kwargs)` call sets, in the order Django writes
    them (the order given), or `None` if one of them must go to Django.

    A plain value is prepared like Django's `UPDATE` compiler prepares it
    (`get_db_prep_save()`); an expression (`F("pages") + 1`) is read like any
    other. The step isn't added to a recipe: `update()` runs it right away.
    """
    if not kwargs:
        return None  # Django runs no SQL for `update()` with nothing to set
    reader = ArgumentReader(model, {})
    shapes = []
    for name, value in kwargs.items():
        field = updated_field(model, name)
        if field is None:
            return None
        if value is None or isinstance(value, bool):
            shapes.append((name, reader.shape_of(value)))
        elif hasattr(value, "resolve_expression"):
            shapes.append((name, reader.shape_of(value, for_save=True)))
        else:
            if isinstance(value, models.Model) or prepare(field, value) is NOT_SIMPLE:
                return None  # Django sets a model instance's primary key in its own way; an unusual type...
            # Django's `UPDATE` compiler prepares the value with `get_db_prep_save()`, when it writes the SQL.
            reader.values.append(QueryValue(value, field, "save", is_plain=True))
            shapes.append((name, type(value)))
    if reader.found_something_not_simple:
        return None
    return Step(
        "update",
        (),
        dict(kwargs),
        ("update", tuple(shapes)),
        tuple(reader.values),
        uses_expressions=reader.uses_expressions,
    )


def slice_step(low: int | None, high: int | None) -> Step:
    """The step of `queryset[low:high]`. Django writes the slice into the
    SQL (`LIMIT 20`), so it is part of the shape."""
    return Step("slice", (low, high), shape=("slice", low, high))


def reverse_step() -> Step:
    """The step of `reverse()`."""
    return Step("reverse", shape=("reverse",))


def order_cleared_step() -> Step:
    """The step of `order_by()` with no names, which `get()` adds (no check
    needed: Django accepts it on any query it can filter)."""
    return Step("order_by", shape=("order_by", (), ()))


def timezone_names() -> tuple[str, ...]:
    """The current time zone, when some of Django's expressions read it
    (`TruncDay()`, `ExtractHour()` with `USE_TZ = True`): part of the
    statement's key for a query with expressions (`statements.py`)."""
    if not settings.USE_TZ:
        return ()
    return (timezone.get_current_timezone_name(),)


# ---------------------------------------------------------------- reading arguments


class ArgumentReader:
    """Reads the arguments of one call: their shapes, and the values met on
    the way, in order (`values`), like reading a sentence word by word.

    `annotation_fields` are the annotations made by earlier calls, which
    filters may name (`n__gt=2`).
    """

    def __init__(
        self, model: type[models.Model], annotation_fields: "dict[str, models.Field[Any, Any] | None]"
    ) -> None:
        self.model = model
        self.annotation_fields = annotation_fields
        self.values: list[QueryValue] = []
        # Set when the reader meets something the fast lane leaves to
        # Django; the caller then gives up on the whole call.
        self.found_something_not_simple = False
        # Set when the call needs Django's check (see `Step.needs_check`).
        self.needs_check = False
        # Set when the call has expressions (see `Step.uses_expressions`).
        self.uses_expressions = False

    # ------------------------------------------------------------ filters

    def where_for(self, children: list[Any], connector: str, negated: bool) -> Where | None:
        """The `Where` for the children of a `Q` (or of a `filter()` call), or `None`."""
        if connector not in (Q.AND, Q.OR) or not children:
            return None  # `XOR` has SQL of its own on some databases; an empty `Q()` adds nothing
        noted: list[Condition | Where] = []
        for child in children:
            node: Condition | Where | None = None
            # A `Q` exactly: a subclass may build its filters its own way.
            if type(child) is Q:
                node = self.where_for(list(child.children), child.connector, child.negated)
            elif type(child) is tuple and len(child) == 2 and isinstance(child[0], str):
                node = self.condition_for(child[0], child[1])
            # Anything else (an expression like `Exists(...)`) leaves `node` at `None`.
            if node is None:
                return None
            noted.append(node)
        return Where(tuple(noted), connector, negated)

    def condition_for(self, name: str, value: Any) -> Condition | None:
        """The `Condition` for one filter, like `pages__gt=500`, or `None` if
        Django should handle it."""
        found = self.field_and_lookup(name)
        if found is None:
            return None
        field, lookup, lookup_class, is_plain = found
        if not is_plain:
            self.needs_check = True  # a filter on an annotation: Django may refuse some
        # A copy of a list or set (`title__in=titles`): Django reads the values
        # when `filter()` runs, so changing `titles` afterwards changes nothing.
        given = type(value)(value) if type(value) in (list, set) else value
        if lookup == "isnull":
            if type(value) is not bool:  # Django raises for anything else
                return None
            return Condition(name, given, "isnull", (), detail=value)
        if value is None:
            # Django reads `exact=None` as `isnull=True`, and refuses `None` for the other lookups.
            return Condition(name, given, "isnull", (), detail=True) if lookup == "exact" else None
        if hasattr(value, "resolve_expression") and lookup != "in":
            return self.expression_condition(name, value, lookup)
        # Django's lookups prepare a relation's value with the field it points to.
        target_field = getattr(field, "target_field", None) or field
        integer_range = issubclass(lookup_class, lookups.IntegerFieldOverflow)
        if lookup == "in":
            prepared_values = prepare_in_values(field, value)
            if prepared_values is None:
                return None
            values = tuple(
                QueryValue(item, target_field, "lookup", is_plain, integer_range) for item in prepared_values
            )
            # Under `exclude()`, Django writes `OR "column" IS NULL` when the values include `None`.
            includes_none = any(item is None for item in value)
            return Condition(name, given, "in", values, detail=(len(values), includes_none))
        prepared = prepare(field, value)
        if prepared is NOT_SIMPLE:
            return None
        query_value = QueryValue(prepared, target_field, "lookup", is_plain, integer_range)
        return Condition(name, given, lookup, (query_value,), detail=prepared if isinstance(prepared, bool) else None)

    def expression_condition(self, name: str, expression: Any, lookup: str) -> Condition | None:
        """The `Condition` for a filter compared with an expression, like
        `pages__gt=F("chapters")`. Django checks it once (a wrong name in
        `F()` raises when `filter()` runs)."""
        first_value = len(self.values)
        shape = self.shape_of(expression)
        if self.found_something_not_simple:
            return None
        self.needs_check = True
        values = tuple(self.values[first_value:])
        del self.values[first_value:]  # they belong to the condition, which the `Where` collects
        return Condition(name, expression, lookup, values, detail=("expression", shape))

    def field_and_lookup(
        self, name: str
    ) -> "tuple[models.Field[Any, Any], str, type[lookups.Lookup[Any]], bool] | None":
        """The field, the lookup name and Django's lookup class for a filter
        name, and whether it is a plain filter on the model's fields (see
        `QueryValue.is_plain`), or `None`.

        A name starting with an annotation's name (`n__gt`) compares the
        annotation, through its output field, like Django does.
        """
        parts = name.split(LOOKUP_SEP)
        if parts[0] in self.annotation_fields:
            field = self.annotation_fields[parts[0]]
            if field is None or len(parts) > 2:
                return None  # an unknown output field, or a transform
            found = known_lookup(field, parts[1] if len(parts) == 2 else "exact")
            if found is None:
                return None
            return field, found[0], found[1], False
        found_on_model = model_field_and_lookup(self.model, name)
        if found_on_model is None:
            return None
        field, lookup, lookup_class = found_on_model
        return field, lookup, lookup_class, True

    # ------------------------------------------------------------ expressions

    def shape_of(self, argument: Any, for_save: bool = False) -> Any:
        """The shape of one argument of a call: the argument, with its values
        left out (they are added to `values`). `for_save`: the argument is a
        value `update()` sets, which Django prepares with `get_db_prep_save()`.

        If the argument has anything the fast lane leaves to Django, sets
        `found_something_not_simple` (the shape returned then doesn't matter).
        """
        if argument is None:
            return argument
        if isinstance(argument, str):
            # A name, like `"-pages"`, or the `"chapters"` of `Count("chapters")`
            # (or a word like the `"day"` of `Trunc("published", "day")`).
            if names_a_transform(self.model, argument):
                return self.not_simple()
            return argument
        if isinstance(argument, bool):
            # `(bool, True)`, not `True`: in Python `True == 1`, and the shape of a `1` is `int`.
            return (bool, argument)
        argument_type = type(argument)
        if argument_type in EXPRESSION_VALUE_FIELDS:
            # A number or a date among an expression's arguments, like the `1` of
            # `Substr("title", 1, 3)`: Django makes it a `Value()`.
            prepared_as = "save" if for_save else "value"
            self.values.append(QueryValue(argument, EXPRESSION_VALUE_FIELDS[argument_type], prepared_as))
            return argument_type
        if argument_type in (list, tuple):
            return (argument_type, tuple(self.shape_of(item, for_save) for item in argument))
        if argument_type is dict:
            return (dict, tuple((key, self.shape_of(item, for_save)) for key, item in argument.items()))
        if argument_type is Q:
            # A condition inside an expression, like `Count("chapters", filter=Q(chapters__number=1))`.
            where = self.where_for(list(argument.children), argument.connector, argument.negated)
            if where is None:
                return self.not_simple()
            self.values.extend(where.values)
            return (Q, where.shape)
        if isinstance(argument, models.Field):
            return field_shape(argument)  # like `output_field=DecimalField(max_digits=5)`
        if is_djangos_expression(argument):
            return self.expression_shape(argument, for_save)
        return self.not_simple()

    def expression_shape(self, expression: Any, for_save: bool) -> Any:
        """The shape of one of Django's expressions: its class and the shapes
        of the arguments it was made with."""
        self.uses_expressions = True
        self.needs_check = True
        if type(expression) is models.Value:
            return self.value_shape(expression, for_save)
        if type(expression) is When:
            # `When(pages__gt=500, then=...)` turns its filters into a `Q`
            # (`condition`), prepared like any filter.
            return (When, self.shape_of(expression.condition, for_save), self.shape_of(expression.result, for_save))
        # Every Django expression keeps the arguments it was made with
        # (`_constructor_args`, for `deconstruct()`), like `(("chapters",), {"distinct": True})`.
        made_with = getattr(expression, "_constructor_args", None)
        if made_with is None:
            return self.not_simple()
        args, kwargs = made_with
        return (type(expression), self.shape_of(args, for_save), self.shape_of(kwargs, for_save))

    def value_shape(self, expression: models.Value, for_save: bool) -> Any:
        """The shape of a `Value(...)`: its type and output field. Its value is a value."""
        value = expression.value
        _args, kwargs = expression._constructor_args  # type: ignore[attr-defined]  # Django's, see above
        output_field_shape = self.shape_of(kwargs.get("output_field"))
        if value is None or isinstance(value, bool):
            return (models.Value, self.shape_of(value), output_field_shape)
        # `_output_field_or_none`: the `output_field` given, or the one Django picks for the value's type.
        output_field = expression._output_field_or_none  # type: ignore[attr-defined]  # Django's, private
        if output_field is None:
            return self.not_simple()
        self.values.append(QueryValue(value, output_field, "save" if for_save else "value"))
        return (models.Value, type(value), output_field_shape)

    def not_simple(self) -> Any:
        """Note that this call has something the fast lane leaves to Django."""
        self.found_something_not_simple = True
        return NOT_SIMPLE


def is_djangos_expression(argument: Any) -> bool:
    """Whether `argument` is one of Django's own expressions (`F`, `Count`,
    `Case`...), but not a subquery or a filter written as an expression.

    The SQL of Django's expressions depends only on their arguments (and the
    time zone, see `timezone_names()`). An expression class of the
    program's own may write any SQL, so it gets Django's way.
    """
    if not hasattr(argument, "resolve_expression"):
        return False
    if isinstance(argument, (models.QuerySet, models.Model, lookups.Lookup)):
        return False  # a subquery, or a lookup as an expression (it prepares its value by field)
    return type(argument).__module__.startswith("django.")


def field_shape(field: "models.Field[Any, Any]") -> Any:
    """The shape of a field given to an expression, like `DecimalField(max_digits=5)`:
    its class and arguments (`deconstruct()`), which decide the SQL Django writes."""
    _name, path, args, kwargs = field.deconstruct()
    # `make_hashable()` turns lists and dicts into tuples, so the shape can be a dict key.
    return ("field", path, make_hashable(args), make_hashable(kwargs))


# ---------------------------------------------------------------- filter values


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


def prepare(field: Any, value: Any) -> Any:
    """`value` prepared by `field`, like Django's lookup prepares it, or
    `NOT_SIMPLE` if Django should handle it.

    `field` is a model field, or a relation (a foreign key, a reverse
    relation, a many-to-many field): then the value is a related object or
    its primary key, prepared by the field the relation points to.
    """
    if hasattr(value, "resolve_expression"):  # F(), Value(), a subquery, a QuerySet...
        return NOT_SIMPLE
    value_field = field
    if field.is_relation:
        # `author=some_author` means `author_id=some_author.pk` (Django's `get_normalized_value()`).
        value_field = field.target_field
        if isinstance(value, models.Model):
            related_model = field.related_model
            if type(value)._meta.concrete_model is not related_model._meta.concrete_model:
                return NOT_SIMPLE  # Django raises "Cannot query ...: Must be ... instance."
            value = getattr(value, value_field.attname)
            if value is None:
                return NOT_SIMPLE  # Django raises: the instance isn't saved
    elif isinstance(value, models.Model):
        return NOT_SIMPLE
    allowed_types = VALUE_TYPES.get(value_field.get_internal_type())
    if allowed_types is None or type(value) not in allowed_types:
        return NOT_SIMPLE
    return value_field.get_prep_value(value)


# ---------------------------------------------------------------- names


@lru_cache(maxsize=4096)
def model_field_and_lookup(
    model: type[models.Model], name: str
) -> "tuple[models.Field[Any, Any], str, type[lookups.Lookup[Any]]] | None":
    """The field, the lookup name and Django's lookup class for a filter
    name like `"pages__gt"`, `"pk"`, `"author"`, `"chapters__title__in"`, or `None`.

    Cached per model and name: models don't change while the program runs.
    (`@lru_cache` remembers what a function returned for each argument.)
    """
    found = field_at_path(model, name.split(LOOKUP_SEP))
    if found is None:
        return None
    field, lookup_parts = found
    if len(lookup_parts) > 1:
        return None  # a transform, like `published__year__gt`
    found_lookup = known_lookup(field, lookup_parts[0] if lookup_parts else "exact")
    if found_lookup is None:
        return None
    return field, found_lookup[0], found_lookup[1]


def known_lookup(field: Any, lookup: str) -> "tuple[str, type[lookups.Lookup[Any]]] | None":
    """The lookup name and Django's lookup class for `field__<lookup>`, if
    the fast lane knows that class (`KNOWN_LOOKUP_CLASSES`)."""
    if lookup not in KNOWN_LOOKUP_CLASSES:
        return None
    lookup_class = field.get_lookup(lookup)
    if lookup_class not in KNOWN_LOOKUP_CLASSES[lookup]:
        return None
    return lookup, lookup_class


@lru_cache(maxsize=4096)
def names_a_transform(model: type[models.Model], name: str) -> bool:
    """Whether `name` (like `"price__floor"` or `"-title__lower"`) names a field
    of `model`, then a transform of it.

    Django finds a transform among the lookups registered at the moment it
    reads the name (`register_lookup()`), and the fast lane reads names
    later, when the query runs. So names with transforms are left to Django,
    like in filters. A name that isn't a field (an annotation, a word like
    `"day"`) is not a transform.
    """
    found = field_at_path(model, name.removeprefix("-").split(LOOKUP_SEP))
    return found is not None and len(found[1]) > 0


def field_at_path(model: type[models.Model], parts: list[str]) -> "tuple[Any, list[str]] | None":
    """The field the start of `parts` names, following relations, and the
    parts left after it (the lookup). `None` if a part isn't a field
    `model_field()` accepts.

    `["author", "name", "in"]` on `Book` gives `Author.name` and `["in"]`.
    Like Django, a part is a field name if the model has such a field, and
    a lookup otherwise: only a relation leads to another model's fields.
    """
    field = model_field(model, parts[0])
    if field is None:
        return None
    position = 1
    while position < len(parts) and field.is_relation:
        # `related_model` is the model the relation points to (`Author` for `Book.author`).
        next_field = model_field(field.related_model, parts[position])
        if next_field is None:
            break  # not a field of the related model: a lookup on the relation, like `author__in`
        field = next_field
        position += 1
    return field, parts[position:]


def model_field(model: type[models.Model], name: str) -> Any:
    """The field `name` (or `"pk"`) stands for, if a filter may name it.

    That is one column of the model's own table (a plain field or a foreign
    key, by name or by column like `author_id`), or a relation to other rows:
    a reverse relation (`author.books`, named `books` in filters) or a
    many-to-many field. Not a primary key made of several columns, or a
    generic relation.
    """
    meta = model._meta
    try:
        field = meta.pk if name == "pk" else meta.get_field(name)
    except FieldDoesNotExist:
        return None
    if isinstance(field, models.ForeignObjectRel):
        # The reverse side of a foreign key, a one-to-one or a many-to-many field.
        return field if single_column(field.field) else None
    if not isinstance(field, models.Field) or (field.column is None and not field.many_to_many):
        return None
    if field.many_to_many:
        return field if isinstance(field, models.ManyToManyField) else None
    if not field.concrete or (field.is_relation and not single_column(field)):
        return None
    return field


def single_column(field: Any) -> bool:
    """Whether a relation joins on one column (not a composite foreign key)."""
    if isinstance(field, models.ManyToManyField):
        return True
    return len(field.foreign_related_fields) == 1


def updated_field(model: type[models.Model], name: str) -> "models.Field[Any, Any] | None":
    """The field `update(name=...)` sets, if it is one plain column of the
    model's own table. Django's `UPDATE` compiler writes those as `"column" = %s`."""
    if name == "pk":
        return None  # Django refuses `pk` in some cases: let it decide
    field = model_field(model, name)
    if not isinstance(field, models.Field) or field.many_to_many or field.generated:
        return None  # Django skips generated fields, and refuses relations to many rows
    if field.model._meta.concrete_model is not model._meta.concrete_model:
        return None  # a parent model's field: another table
    if hasattr(field, "get_placeholder"):
        return None  # the field writes its own SQL for its values
    return field
