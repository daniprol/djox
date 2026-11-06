"""`QueryRecipe`: the calls that make a query, written down instead of building Django's `Query`.

`Book.objects.annotate(n=Count("chapters")).filter(pages__lt=500).order_by("-n")[:20]`
makes Django build a `Query` object at every step: it turns names into
joins and columns, picks a lookup class for each filter, and copies the whole
thing for the next step. Most programs run the same few kinds of queries
over and over, only with other values. So the fast lane writes the calls
down instead, like a recipe, and leaves the cooking to later:

```python
QueryRecipe(
    steps=(
        Step("annotate", kwargs={"n": Count("chapters")}),
        Step("filter", kwargs={"pages__lt": 500}),
        Step("order_by", args=("-n",)),
        Step("slice", args=(None, 20)),
    )
)
```

Each step has a **shape** and **values**. The values are what changes from
one query to the next (the `500`); the shape is everything else (the names,
the expressions, the slice). Queries with the same shape have the same SQL,
so Django writes it once per shape (`statements.py`), and later queries only
need their values turned into parameters (`QueryValue.parameter()`).

`djox.QuerySet` keeps a recipe while it can (`_recipe`). Django's `Query` is
built from it only when something asks for `queryset.query`
(`django_queryset()`): each step is the same call on a plain Django
`QuerySet`, so every method the fast lane doesn't speed up still works
exactly as in Django.

A recipe is never changed: `with_step()` returns a new one, the way each
`QuerySet` method returns a new `QuerySet`.

The arguments of each call are read by `arguments.py`. A `filter()` call
keeps more than its shape and values: a `Where` tree, like Django's `Q`, so
the fast lane knows when nothing can match (`id__in=[]`) without asking.
"""

from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

from django.db import models
from django.db.models import Q

if TYPE_CHECKING:
    from django.db.models import Field, Model


@dataclass(frozen=True)
class QueryValue:
    """One value given to a query, like the `500` of `filter(pages__lt=500)`,
    and how Django turns it into a parameter of the SQL (`parameter()`).

    Django prepares a value in one of three ways, depending on where it is:

    | `prepared_as` | Where the value is                | Django calls                                 |
    |---------------|-----------------------------------|----------------------------------------------|
    | `"lookup"`    | a filter: `pages__lt=500`         | `get_db_prep_value(prepared=True)` (1)       |
    | `"value"`     | an expression: `F("pages") + 1`   | `get_db_prep_value()` of the field Django picks for its type |
    | `"save"`      | what `update()` sets: `pages=500` | `get_db_prep_save()`                         |

    (1) after `get_prep_value()`, which ran when `filter()` was called.
    """

    value: Any
    """The value: already prepared by the field for a lookup (`"5"` became
    `5`), as given otherwise."""

    field: "Field[Any, Any]"
    """The field whose methods make the parameter: the compared field (for a
    foreign key, the field it points to), the updated field, or for a value
    in an expression the field Django picks for its type (`IntegerField` for `1`)."""

    prepared_as: str
    """`"lookup"`, `"value"` or `"save"` (see the table above)."""

    is_plain: bool = False
    """Whether Django writes this value in the order of the filters: it is a
    filter on one of the model's fields (in the `WHERE` clause), or a value
    `update()` sets (in the `SET` clause, before the filters). Values of
    expressions and filters on annotations may move to other clauses
    (`SELECT`, `HAVING`, `ORDER BY`), even more than once."""

    checks_integer_range: bool = False
    """Whether Django's lookup refuses integers the column can't hold (it then
    knows the answer without asking). The fast lane leaves those to Django."""

    def parameter(self, connection: Any) -> Any:
        """The parameter Django would send to the database for this value.
        `connection` is the database's Django connection (its `DatabaseWrapper`)."""
        if self.prepared_as == "lookup":
            return self.field.get_db_prep_value(self.value, connection, prepared=True)
        if self.prepared_as == "save":
            return self.field.get_db_prep_save(self.value, connection)
        return self.field.get_db_prep_value(self.value, connection)

    @property
    def is_boolean(self) -> bool:
        """Whether the value is `True` or `False`. Those are part of the shape
        too: some databases write them into the SQL (`WHERE "in_print"`)."""
        return isinstance(self.value, bool)


@dataclass(frozen=True)
class Condition:
    """One filter, like `pages__gt=500`, `author__name="Jane Austen"` or `pages__gt=F("chapters")`."""

    name: str
    """The name as given, like `"pages__gt"`: it says which relations Django
    joins, which field it compares, and how."""

    value: Any
    """The value as given (a copy, for a list or a set), to give Django the
    same filter when it builds its `Query` from the recipe."""

    lookup: str
    """Django's lookup name: `"exact"`, `"gt"`, `"gte"`, `"lt"`, `"lte"`, `"in"` or `"isnull"`."""

    values: tuple[QueryValue, ...]
    """The values that become the statement's parameters: one, several for
    `in`, none for `isnull`, those of the expression for `F("pages") + 1`."""

    detail: Any = None
    """What, besides the name and the lookup, changes the SQL: for an `in`, how
    many values it has and whether `None` was one of them; `True` or `False`
    for `isnull`; the value itself for a `True` or `False` (some databases
    write `WHERE "in_print"` for it); the shape of an expression."""

    @property
    def shape(self) -> tuple[Any, ...]:
        """The part of the query's shape this condition adds."""
        return (self.name, self.lookup, self.detail)

    @property
    def matches_nothing(self) -> bool:
        """`in` with no values: nothing can match, so no SQL needs to run (like Django).
        (Not `in` with a subquery, whose values are the subquery's.)"""
        if self.lookup != "in":
            return False
        value_count = self.detail[0]  # or `"expression"`, for a subquery
        return bool(value_count == 0)

    def as_q_child(self) -> tuple[str, Any]:
        """This condition as a child of Django's `Q`: `("pages__gt", 500)`."""
        return (self.name, self.value)


@dataclass(frozen=True)
class FilterExpression:
    """A filter written as an expression: `filter(Exists(...))`, `~Exists(...)`."""

    expression: Any
    """The expression as given, to give Django the same filter when it builds its `Query`."""

    values: tuple[QueryValue, ...]
    """The expression's values (for `Exists()`, those of its subquery)."""

    expression_shape: Any
    """The expression with its values left out (see `arguments.py`)."""

    @property
    def shape(self) -> tuple[Any, ...]:
        """The part of the query's shape this filter adds."""
        return ("expression", self.expression_shape)

    @property
    def matches_nothing(self) -> bool:
        """Only Django knows, when it writes the SQL."""
        return False

    def as_q_child(self) -> Any:
        """This filter as a child of Django's `Q`: the expression itself."""
        return self.expression


@dataclass(frozen=True)
class Where:
    """Conditions joined by `AND` or `OR`, maybe negated: the fast lane's `Q` object.

    A `filter()` call is one (`AND`), an `exclude()` call too (`AND`, negated),
    and so is each `Q` inside them. So `exclude(Q(pages__gt=500) | Q(in_print=False))` is:

    ```
    Where(
        negated=True,
        children=(Where(connector="OR", children=(Condition("pages__gt", ...), Condition("in_print", ...))),),
    )
    ```

    The structure decides the SQL (`AND` or `OR`, `NOT`, which joins are
    `LEFT OUTER`), so it is part of the query's shape. The values are not.
    """

    # `"Condition | FilterExpression | Where"`: each child is a condition, an
    # expression like `Exists(...)`, or another node, like a `Q` in a `Q`.
    children: tuple["Condition | FilterExpression | Where", ...]

    connector: str = "AND"
    """`"AND"` or `"OR"` (Django's `Q.AND` and `Q.OR`)."""

    negated: bool = False
    """Whether the node is negated: `exclude()`, or `~Q(...)`."""

    # `@cached_property` computes the value once, then keeps it on the
    # object, like a property that remembers its answer. A recipe is never
    # changed, so its shape never needs computing again.
    @cached_property
    def shape(self) -> tuple[Any, ...]:
        """The part of the query's shape this node adds."""
        return (self.connector, self.negated, tuple(child.shape for child in self.children))

    @cached_property
    def values(self) -> tuple[QueryValue, ...]:
        """The values of every condition in this node, in the order the SQL has them."""
        values: list[QueryValue] = []
        for child in self.children:
            values.extend(child.values)
        return tuple(values)

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
            if isinstance(child, (Condition, FilterExpression)):
                q_children.append(child.as_q_child())
            else:
                q_children.append(Q(*child.as_q_children(), _connector=child.connector, _negated=child.negated))
        return q_children


@dataclass(frozen=True)
class Step:
    """One call of a recipe, like `filter(pages__lt=500)` or `annotate(n=Count("chapters"))`.

    Made by `arguments.py`, which reads the call's arguments.
    """

    method: str
    """The `QuerySet` method, like `"filter"`; `"slice"` for `queryset[low:high]`."""

    args: tuple[Any, ...] = ()
    """The positional arguments, as given (for `"slice"`: the start and the end)."""

    # `field(default_factory=dict)`: each step gets its own empty dict (a
    # shared `{}` default would be one dict for every step, like in a
    # Python function's default argument).
    kwargs: dict[str, Any] = field(default_factory=dict)
    """The keyword arguments, as given."""

    shape: tuple[Any, ...] = ()
    """The call with its values left out: what decides its SQL."""

    values: tuple[QueryValue, ...] = ()
    """The call's values, in the order `arguments.py` met them."""

    where: Where | None = None
    """For `filter()` and `exclude()`: the filters, as a tree (see `Where`)."""

    needs_check: bool = False
    """Whether Django must check this kind of step once before the fast
    lane takes it (`checks.py`): any step but a plain filter, a slice and
    `reverse()`, whose errors the fast lane raises itself."""

    uses_expressions: bool = False
    """Whether the step has expressions (`F()`, `Count()`...). Some of
    Django's expressions read the current time zone when their SQL is
    written, so it becomes part of the statement's key (`statements.py`)."""

    def apply_to(self, queryset: "models.QuerySet[Any]") -> "models.QuerySet[Any]":
        """This call on `queryset`, a plain Django `QuerySet`: Django's own method."""
        if self.method == "slice":
            return models.QuerySet.__getitem__(queryset, slice(*self.args))
        if self.where is not None:
            # The filters rebuilt from the recipe's copies of the values (a list
            # changed after `filter()` must not change the query, like in Django).
            # `Q(*args, **kwargs)` puts the keyword arguments after the others, sorted:
            # the tree keeps them in that order already.
            arguments = self.where.as_q_children()
            if self.method == "exclude":
                return models.QuerySet.exclude(queryset, *arguments)
            return models.QuerySet.filter(queryset, *arguments)
        # `getattr(models.QuerySet, "annotate")` is Django's own method, called
        # on `queryset` like `Django.annotate(queryset, ...)`.
        method = getattr(models.QuerySet, self.method)
        return method(queryset, *self.args, **self.kwargs)  # type: ignore[no-any-return]


@dataclass(frozen=True)
class QueryRecipe:
    """The calls that make a query (see the module docs).

    An empty `QueryRecipe()` is "all the model's objects", like `Book.objects.all()`.
    """

    steps: tuple[Step, ...] = ()
    """The calls, in order."""

    low: int = 0
    """Where the slices so far start: `[10:20]` skips 10 rows (Django's `low_mark`)."""

    high: int | None = None
    """Where the slices so far end, or `None` for no end (Django's `high_mark`)."""

    annotation_fields: dict[str, "Field[Any, Any] | None"] = field(default_factory=dict)
    """The output field of each annotation (`annotate()`, `alias()`), like
    `IntegerField` for `n=Count("chapters")`, as Django's check found it
    (`checks.py`): filters on annotations are prepared with it. `None` when
    Django can't tell before writing the SQL."""

    groups_rows: bool = False
    """Whether Django's query has a `GROUP BY` (an aggregate annotation, like
    `Count()`): then the model's default ordering doesn't apply."""

    def __deepcopy__(self, memo: dict[int, Any]) -> "QueryRecipe":
        """A recipe never changes, so a deep copy of a `QuerySet` can share it."""
        return self

    def with_step(
        self,
        step: Step,
        annotation_fields: "dict[str, Field[Any, Any] | None] | None" = None,
        groups_rows: bool | None = None,
    ) -> "QueryRecipe":
        """A copy of this recipe with one more call. `annotation_fields` and
        `groups_rows` are what Django's check found after it (`checks.py`),
        when it was checked.

        Every query makes a few recipes, so the copy is made directly, not
        with `dataclasses.replace()` (which checks every field each time): a
        new object made without `__init__` (`__new__`), then its attributes
        set in its `__dict__` (a frozen dataclass only refuses `recipe.low = 5`).
        The remembered properties (`@cached_property`) are not copied: they
        belong to the old recipe.
        """
        low, high = self.low, self.high
        if step.method == "slice":
            low, high = self.limits_after(*step.args)
        copy = object.__new__(QueryRecipe)
        copy.__dict__.update(
            steps=(*self.steps, step),
            low=low,
            high=high,
            annotation_fields=self.annotation_fields if annotation_fields is None else annotation_fields,
            groups_rows=self.groups_rows if groups_rows is None else groups_rows,
        )
        return copy

    def limits_after(self, low: int | None, high: int | None) -> tuple[int, int | None]:
        """Where `[low:high]` of what this recipe returns starts and ends.

        The same arithmetic as Django's `Query.set_limits()`, so a slice of a
        slice ends up where Django's would.
        """
        new_low, new_high = self.low, self.high
        if high is not None:
            new_high = min(new_high, new_low + high) if new_high is not None else new_low + high
        if low is not None:
            new_low = min(new_high, new_low + low) if new_high is not None else new_low + low
        return new_low, new_high

    @cached_property
    def shape(self) -> tuple[Any, ...]:
        """Everything that decides the SQL, and none of the values.

        `filter(pages__gt=500)` and `filter(pages__gt=7)` have the same shape,
        so they share one SQL statement with a different parameter.
        """
        return tuple(step.shape for step in self.steps)

    @cached_property
    def values(self) -> tuple[QueryValue, ...]:
        """Every step's values, in order."""
        values: list[QueryValue] = []
        for step in self.steps:
            values.extend(step.values)
        return tuple(values)

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
        return any(step.where is not None and step.where.matches_nothing for step in self.steps)

    @property
    def is_ordered_by_call(self) -> bool:
        """Whether `order_by()` was called, with names or without (which
        removes the default ordering)."""
        return any(step.method == "order_by" for step in self.steps)

    def has_step(self, method: str) -> bool:
        """Whether one of the calls is to `method`, like `"distinct"`."""
        return any(step.method == method for step in self.steps)

    @cached_property
    def uses_expressions(self) -> bool:
        """Whether a step has expressions (see `Step.uses_expressions`)."""
        return any(step.uses_expressions for step in self.steps)

    def django_queryset(self, model: "type[Model]") -> "models.QuerySet[Any]":
        """A plain Django `QuerySet` of `model` made by these calls, with
        Django's own methods: its `query` is the `Query` Django would have
        built. Raises the error Django would raise for a call."""
        queryset: models.QuerySet[Any] = models.QuerySet(model)
        for step in self.steps:
            queryset = step.apply_to(queryset)
        return queryset
