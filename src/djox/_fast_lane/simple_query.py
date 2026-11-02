"""`SimpleQuery`: what the fast lane writes down instead of building Django's `Query`.

`Book.objects.filter(pages__gt=500).order_by("-pages")[:20]` makes Django
build a `Query` object at every step: it finds the field, picks the lookup
class, adds a node to the `WHERE` tree, copies the whole thing for the next
step. For a query this simple, a note is enough:

```python
SimpleQuery(
    filter_calls=({"pages__gt": 500},),
    conditions=(Condition(pages_field, "gt", (500,)),),
    order_by=("-pages",),
    low=0,
    high=20,
)
```

`djox.QuerySet` keeps such a note while it can (`_simple_query`), and builds
Django's `Query` from it only when something asks for `queryset.query`
(`replay_on()`), so every method djox doesn't speed up still works as in
Django. The fast lane runs the note itself (`djox._fast_lane.running`).

A note is never changed: each step (`with_filter()`, `with_order_by()`...)
returns a new one, the way each `QuerySet` method returns a new `QuerySet`.
"""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from django.db.models import Field, Model, QuerySet


@dataclass(frozen=True)
class Condition:
    """One filter the fast lane understands, like `pages__gt=500`.

    `values` are already prepared by the field (`field.get_prep_value()`, the
    first half of what Django's lookup does): one value, several for `in`,
    none for `isnull`.
    """

    field: "Field[Any, Any]"
    """The model field, like `Book.pages` (for `author=...`, the foreign key)."""

    lookup: str
    """Django's lookup name: `"exact"`, `"gt"`, `"gte"`, `"lt"`, `"lte"`, `"in"` or `"isnull"`."""

    values: tuple[Any, ...]
    """The prepared values that become the statement's parameters."""

    detail: Any = None
    """What, besides the field and the lookup, changes the SQL: how many values
    an `in` has, `True` or `False` for `isnull`, and the value itself for a
    `True` or `False` (some databases write `WHERE "in_print"` for it)."""

    checks_integer_range: bool = False
    """Whether Django's lookup refuses integers the column can't hold (it then
    knows the answer without asking). The fast lane leaves those to Django."""

    @property
    def shape(self) -> tuple[Any, ...]:
        """The part of the query's shape this condition adds (see `SimpleQuery.shape`)."""
        return (self.field, self.lookup, self.detail)

    @property
    def matches_nothing(self) -> bool:
        """`in` with no values: nothing can match, so no SQL needs to run (like Django)."""
        return self.lookup == "in" and not self.values


@dataclass(frozen=True)
class Update:
    """One value `update()` sets, like `pages=500` in `update(pages=500)`."""

    name: str
    """The name given to `update()`: a field's name, or its column (`author_id`)."""

    field: "Field[Any, Any]"
    """The model field it sets."""

    value: Any
    """The value, as given: the field prepares it when the statement runs, like Django."""

    @property
    def shape(self) -> tuple[Any, ...]:
        """What changes the SQL: the field, and whether the value is `None`
        (Django writes `SET "pages" = NULL` for it, with no parameter)."""
        return (self.name, self.field, self.value is None)


@dataclass(frozen=True)
class SimpleQuery:
    """A query made only of what the fast lane understands (see the module docs).

    An empty `SimpleQuery()` is "all the model's objects", like `Book.objects.all()`.
    """

    filter_calls: tuple[dict[str, Any], ...] = ()
    """The keyword arguments of each `filter()` call, exactly as given, to build
    Django's `Query` from them (`replay_on()`)."""

    conditions: tuple[Condition, ...] = ()
    """The same filters, understood: one `Condition` per keyword argument."""

    order_by: tuple[str, ...] | None = None
    """The names given to `order_by()`, like `("-pages", "id")`, or `None` for the
    model's default ordering (`Meta.ordering`)."""

    reverse_ordering: bool = False
    """Whether `reverse()` was called an odd number of times."""

    low: int = 0
    """Where the slice starts: `[10:20]` skips 10 rows (Django's `low_mark`)."""

    high: int | None = None
    """Where the slice ends, or `None` for no end (Django's `high_mark`)."""

    values_fields: tuple[str, ...] | None = None
    """The fields given to `values()` or `values_list()`, like `("title", "pages")`,
    or `None` for model instances. `()` is every field of the model."""

    select_related: tuple[str, ...] = ()
    """The relations given to `select_related()`, like `("author", "author__publisher")`,
    in the order given. `()` for none."""

    def __deepcopy__(self, memo: dict[int, Any]) -> "SimpleQuery":
        """A note never changes, so a deep copy of a `QuerySet` can share it."""
        return self

    @property
    def is_sliced(self) -> bool:
        """Whether a slice was taken (then Django refuses `filter()` and `order_by()`)."""
        return self.low != 0 or self.high is not None

    @property
    def matches_nothing(self) -> bool:
        """Whether Django would know, without asking, that nothing matches:
        an empty slice like `[5:5]`, or `in` with no values."""
        if self.high is not None and self.low == self.high:
            return True
        return any(condition.matches_nothing for condition in self.conditions)

    @property
    def shape(self) -> tuple[Any, ...]:
        """Everything that decides the SQL, and none of the values.

        `filter(pages__gt=500)` and `filter(pages__gt=7)` have the same shape,
        so they share one SQL statement with a different parameter. The slice
        is part of the shape because Django writes `LIMIT 20` into the SQL.
        """
        conditions = tuple(condition.shape for condition in self.conditions)
        return (
            conditions,
            self.order_by,
            self.reverse_ordering,
            self.low,
            self.high,
            self.values_fields,
            self.select_related,
        )

    @property
    def parameters(self) -> list[Any]:
        """Every condition's values, in the order the SQL uses them."""
        parameters: list[Any] = []
        for condition in self.conditions:
            parameters.extend(condition.values)
        return parameters

    def with_filter(self, keyword_arguments: dict[str, Any], conditions: list[Condition]) -> "SimpleQuery":
        """This query, filtered by one more `filter()` call."""
        # A copy of each list or set (`title__in=titles`): Django reads the values
        # when `filter()` runs, so changing `titles` afterwards changes nothing.
        snapshot = {}
        for name, value in keyword_arguments.items():
            snapshot[name] = type(value)(value) if type(value) in (list, set) else value
        return replace(
            self,
            filter_calls=(*self.filter_calls, snapshot),
            conditions=(*self.conditions, *conditions),
        )

    def with_order_by(self, names: tuple[str, ...]) -> "SimpleQuery":
        """This query, ordered by `names` instead (like `order_by()`, which replaces any ordering)."""
        return replace(self, order_by=names)

    def with_reversed_ordering(self) -> "SimpleQuery":
        """This query, in the other order (like `reverse()`)."""
        return replace(self, reverse_ordering=not self.reverse_ordering)

    def with_values(self, fields: tuple[str, ...]) -> "SimpleQuery":
        """This query, returning only `fields` (like `values()` and `values_list()`).

        Without its `select_related()`, like Django's `values()`: dicts and
        tuples have no related objects to cache.
        """
        return replace(self, values_fields=fields, select_related=())

    def with_select_related(self, names: tuple[str, ...]) -> "SimpleQuery":
        """This query, also fetching the objects of the relations `names` (like
        `select_related()`, which adds to the relations given before)."""
        added = tuple(name for name in names if name not in self.select_related)
        return replace(self, select_related=(*self.select_related, *added))

    def without_select_related(self) -> "SimpleQuery":
        """This query, without related objects (like `select_related(None)`)."""
        return replace(self, select_related=())

    def with_limits(self, low: int | None = None, high: int | None = None) -> "SimpleQuery":
        """This query, sliced further: `[low:high]` of what it returns.

        The same arithmetic as Django's `Query.set_limits()`, so a slice of a
        slice ends up where Django's would.
        """
        new_low, new_high = self.low, self.high
        if high is not None:
            new_high = min(new_high, new_low + high) if new_high is not None else new_low + high
        if low is not None:
            new_low = min(new_high, new_low + low) if new_high is not None else new_low + low
        return replace(self, low=new_low, high=new_high)

    def replay_on(self, queryset: "QuerySet[Model]") -> None:
        """Build Django's `Query` of `queryset` from this note, with Django's own methods.

        `queryset`'s query must still be the empty query its `QuerySet` started
        with. Each step does what the matching `QuerySet` method does to its query.
        """
        for keyword_arguments in self.filter_calls:
            queryset._filter_or_exclude_inplace(False, (), keyword_arguments)  # type: ignore[attr-defined]  # Django's
        query = queryset._query  # type: ignore[attr-defined]  # Django's, not in django-stubs
        if self.order_by is not None:
            query.clear_ordering(force=True, clear_default=False)
            query.add_ordering(*self.order_by)
        if self.reverse_ordering:
            query.standard_ordering = not query.standard_ordering
        if self.select_related:
            query.add_select_related(self.select_related)
        if self.is_sliced:
            query.set_limits(self.low, self.high)
        if self.values_fields is not None:
            query.set_values(self.values_fields)
