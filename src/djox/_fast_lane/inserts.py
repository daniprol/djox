"""Inserting new objects on the fast lane: the `INSERT` of `create()` and of `asave()`.

Every new `Book` gets the same SQL:

```sql
INSERT INTO "library_book" ("title", "pages", ...) VALUES (%s, %s, ...) RETURNING "library_book"."id"
```

yet Django builds an `InsertQuery` and compiles it for every object. The
fast lane lets Django write it once per model: the first new object is
saved Django's way (`learn()` keeps the statement Django's code wrote).
After that, `prepare()` only computes the parameters, the way Django's
`INSERT` compiler does: each field's `pre_save()` (which fills `auto_now`
dates), then its `get_db_prep_save()`. Before keeping a statement, the
fast lane checks that it computes the same parameters as Django did, like
for queries (`statements.py`).

The rest of `save()` (its checks, the `pre_save` and `post_save` signals)
is done by the caller, `djox._saving`, with Django's own methods. The fast
lane only takes models whose saving is Django's own, and only an `INSERT`
that Django would make without trying an `UPDATE` first:

| Left to Django                                       | Why                                    |
|------------------------------------------------------|----------------------------------------|
| A model with its own `save()`, `save_base()`...      | it may do anything                     |
| Parent models (multi-table inheritance)              | one `INSERT` per table                 |
| `order_with_respect_to`                              | a query first, for the next `_order`   |
| A composite primary key, generated fields            | other SQL, other steps                 |
| A field with its own `pre_save()` (`FileField`)      | it may write files, or do anything     |
| A field that writes its own SQL (`get_placeholder`)  | the SQL may change with the value      |
| A value that is an expression (`db_default`, `F()`)  | other SQL                              |
| A primary key set, without `force_insert`            | Django tries an `UPDATE` first         |
"""

from dataclasses import dataclass
from typing import Any

from django.db import connections, models

from djox._fast_lane import is_enabled
from djox._fast_lane.statements import LEFT_TO_DJANGO, same_values

# The `pre_save()` methods the fast lane knows: Django's own, which return
# the value to save (the attribute, or the date `auto_now` puts there) and
# do nothing else. A field with another one is left to Django.
KNOWN_PRE_SAVES = {
    models.Field.pre_save,
    models.DateField.pre_save,
    models.DateTimeField.pre_save,
    models.TimeField.pre_save,
}

# The methods of saving that a model must not change, for the fast lane to
# do their work: `create()` calls `save()`, which calls the others.
SAVING_METHODS = ("save", "save_base", "_save_parents", "_save_table", "_do_insert")


@dataclass(frozen=True)
class InsertStatement:
    """The `INSERT` of one model's new objects, as Django wrote it."""

    sql: str
    """Django's SQL, with `%s` placeholders for the parameters."""

    fields: tuple["models.Field[Any, Any]", ...]
    """The fields given values, in the order of the parameters."""

    returning_fields: tuple["models.Field[Any, Any]", ...]
    """The fields the `INSERT` returns (`RETURNING`), usually the new id."""


@dataclass(frozen=True)
class PreparedInsert:
    """One object's `INSERT`, ready to run: the statement and its parameters."""

    statement: InsertStatement
    parameters: list[Any]

    def assign_returned_values(self, obj: models.Model, rows: list[Any]) -> None:
        """Set the values the `INSERT` returned on `obj` (its new id), like Django."""
        if not rows:
            return
        for value, field in zip(rows[0], self.statement.returning_fields, strict=True):
            setattr(obj, field.attname, value)


# The statements by model, alias, and whether the new object has its
# primary key (then it is one of the columns), or `LEFT_TO_DJANGO`.
statements: dict[tuple[Any, ...], InsertStatement | str] = {}


def inserts_like_django(model: type[models.Model]) -> bool:
    """Whether the fast lane can insert `model`'s new objects exactly like
    Django's `save()` would (see the module docs for what it leaves to Django).

    Checked before every save, since a program (or a test) may change a
    model's methods at any time; it is a few attribute lookups.
    """
    for name in SAVING_METHODS:
        if getattr(model, name) is not getattr(models.Model, name):
            return False
    # A proxy model's rows are in the table of the model it proxies.
    meta = model._meta.concrete_model._meta  # type: ignore[union-attr]  # every model has one
    if meta.parents or meta.order_with_respect_to or meta.auto_created:
        return False
    if len(meta.pk_fields) != 1:  # a composite primary key
        return False
    for field in meta.local_concrete_fields:
        if field.generated:
            return False
        # `type(field).pre_save` is the method of the field's class, like `DateTimeField.pre_save`.
        if type(field).pre_save not in KNOWN_PRE_SAVES:
            return False
        if hasattr(field, "get_placeholder") or hasattr(field, "get_placeholder_sql"):
            return False
    return True


def inserts_without_asking(obj: models.Model, force_insert: bool) -> bool:
    """Whether Django's `save()` would `INSERT` `obj` without trying an `UPDATE` first.

    It does when asked to (`force_insert`, which `create()` passes), when
    the object has no primary key yet, or when the object is new and its
    primary key has a default (a `UUIDField(default=uuid.uuid4)`): Django
    then knows no row has it.
    """
    if force_insert:
        return True
    pk_field = obj._meta.pk
    assert pk_field is not None, "a model has a primary key"
    if not is_set(getattr(obj, pk_field.attname)):
        return True
    return obj._state.adding and (pk_field.has_default() or pk_field.has_db_default())


def is_set(pk_value: Any) -> bool:
    """Whether a primary key value is set, like Django's `Model._is_pk_set()`:
    not `None`, and not "the database's default" (`DatabaseDefault`)."""
    return pk_value is not None and not hasattr(pk_value, "resolve_expression")


def prepare(obj: models.Model, model: type[models.Model], alias: str) -> PreparedInsert | None:
    """`obj`'s `INSERT`, ready to run, or `None` to let Django write it this
    time (the statement isn't known yet, or a value is an expression).

    `model` is `obj`'s concrete model (a proxy's table is its concrete
    model's). Like Django's `_save_table()`, this first gives the primary
    key its default, if it has one and no value.
    """
    meta = model._meta
    pk_field = meta.pk
    assert pk_field is not None, "a model has a primary key"
    if not is_enabled():
        return None
    if getattr(obj, pk_field.attname) is None:
        setattr(obj, pk_field.attname, pk_field.get_pk_value_on_save(obj))
    pk_set = is_set(getattr(obj, pk_field.attname))
    statement = statements.get((model, alias, pk_set))
    if not isinstance(statement, InsertStatement):
        return None
    connection = connections[alias]
    parameters = []
    for field in statement.fields:
        # What Django's `INSERT` compiler does for each value (`pre_save_val()`, `prepare_value()`).
        value = field.pre_save(obj, add=True)
        if hasattr(value, "resolve_expression"):
            return None  # `db_default`, `F()`, `Now()`...: other SQL
        parameters.append(field.get_db_prep_save(value, connection))
    return PreparedInsert(statement, parameters)


def learn(obj: models.Model, model: type[models.Model], alias: str, insert_compiler: Any) -> None:
    """Keep the `INSERT` Django's code wrote for `obj` (`insert_compiler`, as
    `compiler.capture()` took it from `save()`), if the fast lane can reuse it.

    Django compiles it here, which calls each field's `pre_save()` again,
    like Django's own `save()` does. The parameters the fast lane would
    compute must be Django's, value by value.
    """
    meta = model._meta
    pk_field = meta.pk
    assert pk_field is not None, "a model has a primary key"
    key = (model, alias, is_set(getattr(obj, pk_field.attname)))
    if key in statements or not is_enabled():
        return
    for field in insert_compiler.query.fields:
        if hasattr(getattr(obj, field.attname), "resolve_expression"):
            return  # this object's SQL is its own (`db_default`...): learn from the next one
    statements[key] = statement_from(obj, model, alias, insert_compiler)


def statement_from(
    obj: models.Model, model: type[models.Model], alias: str, insert_compiler: Any
) -> InsertStatement | str:
    """The `InsertStatement` in `insert_compiler`, or `LEFT_TO_DJANGO`."""
    query = insert_compiler.query
    if query.on_conflict is not None or len(query.objs) != 1:
        return LEFT_TO_DJANGO
    returning_fields = tuple(insert_compiler.returning_fields or ())
    if returning_fields != tuple(model._meta.db_returning_fields):
        return LEFT_TO_DJANGO
    table = model._meta.db_table
    if insert_compiler.get_converters([field.get_col(table) for field in returning_fields]):
        return LEFT_TO_DJANGO  # the returned values need Django's converters
    sql_and_parameters = insert_compiler.as_sql()
    if len(sql_and_parameters) != 1:
        return LEFT_TO_DJANGO
    sql, django_parameters = sql_and_parameters[0]
    connection = connections[alias]
    fields = tuple(query.fields)
    # `as_sql()` just ran each field's `pre_save()`: the attributes hold the values it saved.
    parameters = [field.get_db_prep_save(getattr(obj, field.attname), connection) for field in fields]
    if not same_values(list(django_parameters), parameters):
        return LEFT_TO_DJANGO
    return InsertStatement(sql, fields, returning_fields)
