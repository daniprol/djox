"""`SimpleQuery`: what the fast lane writes down instead of building Django's `Query`.

`Book.objects.filter(pages__gt=500).order_by("-pages")[:20]` makes Django
build a `Query` object at every step: it finds the field, picks the lookup
class, adds a node to the `WHERE` tree, copies the whole thing for the next
step. For a query this simple, a note is enough:

```python
SimpleQuery(
    filters=(Where((Condition("pages__gt", 500, pages_field, "gt", (500,)),)),),
    conditions=(Condition("pages__gt", 500, pages_field, "gt", (500,)),),
    order_by=("-pages",),
    low=0,
    high=20,
)
```

Filters keep the structure Django gives them. Each `filter()` or
`exclude()` call is a `Where`: its conditions joined by `AND`, negated for
`exclude()`. A `Q` object inside it is a `Where` of its own, joined by `AND`
or `OR`, negated by `~`. So `exclude(Q(pages__gt=500) | Q(in_print=False))`
is noted as:

```
Where(
    negated=True, children=(Where(connector="OR", children=(Condition("pages__gt", ...), Condition("in_print", ...))),)
)
```

The structure decides the SQL (`AND` or `OR`, `NOT`, which joins are
`LEFT OUTER`), so it is part of the query's shape. The values are not.

`djox.QuerySet` keeps such a note while it can (`_simple_query`), and builds
Django's `Query` from it only when something asks for `queryset.query`
(`replay_on()`), so every method djox doesn't speed up still works as in
Django. The fast lane runs the note itself (`djox._fast_lane.running`).

A note is never changed: each step (`with_filter()`, `with_order_by()`...)
returns a new one, the way each `QuerySet` method returns a new `QuerySet`.
"""

from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Any

from django.db.models import Q

if TYPE_CHECKING:
    from django.db.models import Field, Model, QuerySet


@dataclass(frozen=True)
class Condition:
    """One filter the fast lane understands, like `pages__gt=500` or `author__name="Jane Austen"`.

    `values` are already prepared by the field (`field.get_prep_value()`, the
    first half of what Django's lookup does): one value, several for `in`,
    none for `isnull`.
    """

    name: str
    """The name as given, like `"pages__gt"` or `"author__name"`: it says
    which relations Django joins, which field it compares, and how."""

    value: Any
    """The value as given (a copy, for a list or a set), to give Django the
    same filter when it builds its `Query` from the note."""

    field: "Field[Any, Any]"
    """The model field compared, like `Book.pages`, or `Author.name` for
    `author__name` (for `author=...`, the foreign key)."""

    lookup: str
    """Django's lookup name: `"exact"`, `"gt"`, `"gte"`, `"lt"`, `"lte"`, `"in"` or `"isnull"`."""

    values: tuple[Any, ...]
    """The prepared values that become the statement's parameters."""

    detail: Any = None
    """What, besides the name and the lookup, changes the SQL: for an `in`, how
    many values it has and whether `None` was one of them; `True` or `False`
    for `isnull`; and the value itself for a `True` or `False` (some
    databases write `WHERE "in_print"` for it)."""

    checks_integer_range: bool = False
    """Whether Django's lookup refuses integers the column can't hold (it then
    knows the answer without asking). The fast lane leaves those to Django."""

    @property
    def shape(self) -> tuple[Any, ...]:
        """The part of the query's shape this condition adds (see `SimpleQuery.shape`)."""
        return (self.name, self.lookup, self.detail)

    @property
    def matches_nothing(self) -> bool:
        """`in` with no values: nothing can match, so no SQL needs to run (like Django)."""
        return self.lookup == "in" and not self.values

    def as_q_child(self) -> tuple[str, Any]:
        """This condition as a child of Django's `Q`: `("pages__gt", 500)`."""
        return (self.name, self.value)


@dataclass(frozen=True)
class Where:
    """Conditions joined by `AND` or `OR`, maybe negated: the fast lane's `Q` object.

    A `filter()` call is one (`AND`), an `exclude()` call too (`AND`, negated),
    and so is each `Q` inside them (see the module docs).
    """

    # `"Condition | Where"`: each child is a condition or another node, like a `Q` in a `Q`.
    children: tuple["Condition | Where", ...]

    connector: str = "AND"
    """`"AND"` or `"OR"` (Django's `Q.AND` and `Q.OR`)."""

    negated: bool = False
    """Whether the node is negated: `exclude()`, or `~Q(...)`."""

    # `@cached_property` computes the value once, then keeps it on the
    # object, like a property that remembers its answer. A note is never
    # changed, so its shape never needs computing again.
    @cached_property
    def shape(self) -> tuple[Any, ...]:
        """The part of the query's shape this node adds (see `SimpleQuery.shape`)."""
        return (self.connector, self.negated, tuple(child.shape for child in self.children))

    @property
    def conditions(self) -> list[Condition]:
        """Every condition in this node, in the order the SQL has them."""
        conditions: list[Condition] = []
        for child in self.children:
            if isinstance(child, Condition):
                conditions.append(child)
            else:
                conditions.extend(child.conditions)
        return conditions

    @property
    def matches_nothing(self) -> bool:
        """Whether Django knows, without asking, that nothing matches this node.

        An `in` with no values matches nothing; so does an `AND` with one
        such child, and an `OR` whose children all are. Under a `NOT`, the
        answer is "everything", which still needs SQL (Django then leaves
        the node out of the `WHERE`).
        """
        if self.negated:
            return False
        if self.connector == "AND":
            return any(child.matches_nothing for child in self.children)
        return all(child.matches_nothing for child in self.children)

    def as_q_children(self) -> list[Any]:
        """The children of the `Q` this node stands for: `Q` objects and `(name, value)` pairs."""
        q_children: list[Any] = []
        for child in self.children:
            if isinstance(child, Condition):
                q_children.append(child.as_q_child())
            else:
                q_children.append(Q(*child.as_q_children(), _connector=child.connector, _negated=child.negated))
        return q_children


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

    filters: tuple[Where, ...] = ()
    """One `Where` per `filter()` or `exclude()` call, in the order of the calls."""

    conditions: tuple[Condition, ...] = ()
    """Every condition of `filters`, in the order the SQL has them (and so
    their parameters). Kept apart so each query doesn't walk the tree again."""

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

    def changed(self, **changes: Any) -> "SimpleQuery":
        """A copy of this note with some fields changed, like `dataclasses.replace()`.

        `replace()` checks every field each time, which made it the slowest
        step of a simple query (2.5 µs, three times per `get()`). Here the
        copy is made directly: a new object without `__init__` (`__new__`),
        then its attributes (`__dict__`) copied over and changed. A frozen
        dataclass only refuses changes made with `note.field = value`.
        """
        copy = object.__new__(SimpleQuery)
        copy.__dict__.update(self.__dict__)
        copy.__dict__.update(changes)
        return copy

    @property
    def is_sliced(self) -> bool:
        """Whether a slice was taken (then Django refuses `filter()` and `order_by()`)."""
        return self.low != 0 or self.high is not None

    @property
    def matches_nothing(self) -> bool:
        """Whether Django would know, without asking, that nothing matches:
        an empty slice like `[5:5]`, or an `in` with no values that decides
        the answer (see `Where.matches_nothing`)."""
        if self.high is not None and self.low == self.high:
            return True
        return any(where.matches_nothing for where in self.filters)

    @property
    def shape(self) -> tuple[Any, ...]:
        """Everything that decides the SQL, and none of the values.

        `filter(pages__gt=500)` and `filter(pages__gt=7)` have the same shape,
        so they share one SQL statement with a different parameter. The slice
        is part of the shape because Django writes `LIMIT 20` into the SQL.
        """
        filters = tuple(where.shape for where in self.filters)
        return (
            filters,
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

    def with_filter(self, where: Where) -> "SimpleQuery":
        """This query, filtered by one more `filter()` or `exclude()` call."""
        return self.changed(filters=(*self.filters, where), conditions=(*self.conditions, *where.conditions))

    def with_order_by(self, names: tuple[str, ...]) -> "SimpleQuery":
        """This query, ordered by `names` instead (like `order_by()`, which replaces any ordering)."""
        return self.changed(order_by=names)

    def with_reversed_ordering(self) -> "SimpleQuery":
        """This query, in the other order (like `reverse()`)."""
        return self.changed(reverse_ordering=not self.reverse_ordering)

    def with_values(self, fields: tuple[str, ...]) -> "SimpleQuery":
        """This query, returning only `fields` (like `values()` and `values_list()`).

        Without its `select_related()`, like Django's `values()`: dicts and
        tuples have no related objects to cache.
        """
        return self.changed(values_fields=fields, select_related=())

    def with_select_related(self, names: tuple[str, ...]) -> "SimpleQuery":
        """This query, also fetching the objects of the relations `names` (like
        `select_related()`, which adds to the relations given before)."""
        added = tuple(name for name in names if name not in self.select_related)
        return self.changed(select_related=(*self.select_related, *added))

    def without_select_related(self) -> "SimpleQuery":
        """This query, without related objects (like `select_related(None)`)."""
        return self.changed(select_related=())

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
        return self.changed(low=new_low, high=new_high)

    def replay_on(self, queryset: "QuerySet[Model]") -> None:
        """Build Django's `Query` of `queryset` from this note, with Django's own methods.

        `queryset`'s query must still be the empty query its `QuerySet` started
        with. Each step does what the matching `QuerySet` method does to its query.
        """
        for where in self.filters:
            # What `filter(*args, **kwargs)` does, with the same `Q` children
            # (`Q(*args, **kwargs)` puts the keyword arguments after the others, sorted).
            arguments = where.as_q_children()
            queryset._filter_or_exclude_inplace(where.negated, arguments, {})  # type: ignore[attr-defined]  # Django's
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
