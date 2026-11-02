"""`djox.ResultBuilder` (Rust): rows to model instances, like Django's
`from_db()` (with the objects of `select_related()`, like Django's
`RelatedPopulator`), and to the tuples and dicts of `values_list()` and `values()`."""

from typing import Any

import pytest
from django.db import connection
from django.db.models.base import ModelState

from djox._core import RelatedModel, ResultBuilder
from tests.e2e.library.models import Author, Book

ATTNAMES = ["id", "name"]


def test_it_builds_what_from_db_builds() -> None:
    builder = ResultBuilder(ATTNAMES, [None, None], [None, None], connection, Author, ModelState)
    [built] = builder.instances([(1, "Frank Herbert")], "default")
    django = Author.from_db("default", ATTNAMES, [1, "Frank Herbert"])
    assert type(built) is Author
    assert built.__dict__.keys() == django.__dict__.keys()
    assert (built.id, built.name) == (django.id, django.name)
    assert (built._state.db, built._state.adding) == ("default", False)


def test_each_column_goes_through_its_converters_in_order() -> None:
    calls: list[tuple[Any, ...]] = []

    def strip(value: Any, expression: Any, connection: Any) -> Any:
        calls.append((value, expression, connection))
        return value.strip()

    def shout(value: Any, expression: Any, connection: Any) -> Any:
        return value.upper()

    converters = [None, ([strip, shout], "the name column")]
    builder = ResultBuilder(ATTNAMES, converters, [None, None], connection, Author, ModelState)
    [author] = builder.instances([(1, "  emma  ")], "default")
    assert author.name == "EMMA"
    assert calls == [("  emma  ", "the name column", connection)]


def test_fetch_mode_is_set_on_the_state() -> None:
    builder = ResultBuilder(ATTNAMES, [None, None], [None, None], connection, Author, ModelState)
    marker = object()
    [author] = builder.instances([(1, "x")], "default", marker)
    assert author._state.fetch_mode is marker


def test_a_row_with_the_wrong_number_of_values_is_refused() -> None:
    builder = ResultBuilder(ATTNAMES, [None, None], [None, None], connection, Author, ModelState)
    assert builder.column_count == 2
    with pytest.raises(ValueError, match="a row has 3 values, but there are 2 columns"):
        builder.instances([(1, "x", "extra")], "default")


def test_a_converter_error_reaches_the_caller() -> None:
    def broken(value: Any, expression: Any, connection: Any) -> Any:
        raise ZeroDivisionError("broken converter")

    builder = ResultBuilder(ATTNAMES, [None, ([broken], None)], [None, None], connection, Author, ModelState)
    with pytest.raises(ZeroDivisionError, match="broken converter"):
        builder.instances([(1, "x")], "default")


def test_tuples_and_dicts_hold_the_converted_values() -> None:
    def shout(value: Any, expression: Any, connection: Any) -> Any:
        return value.upper()

    builder = ResultBuilder(["id", "name"], [None, ([shout], None)], [None, None], connection)
    assert builder.tuples([(1, "emma"), (2, "dune")]) == [(1, "EMMA"), (2, "DUNE")]
    assert builder.dicts([(1, "emma")]) == [{"id": 1, "name": "EMMA"}]


def test_instances_need_a_model() -> None:
    builder = ResultBuilder(ATTNAMES, [None, None], [None, None], connection)
    with pytest.raises(TypeError, match="needs a ResultBuilder made with a model"):
        builder.instances([(1, "x")], "default")


# `select_related()`: a book row with its author's columns after its own.
BOOK_AND_AUTHOR = ["id", "title", "author_id", "id", "name"]
NO_CONVERTERS: list[Any] = [None] * len(BOOK_AND_AUTHOR)


def related_author(set_on_related: Any = None) -> RelatedModel:
    """The author of a book, in columns 3 and 4, cached on the book by Django's own setter."""
    author_field: Any = Book._meta.get_field("author")  # `Any`: django-stubs types it as any `Field`
    return RelatedModel(
        Author,
        3,
        5,
        3,
        author_field.set_cached_value,
        set_on_related or (lambda author, book: None),
    )


def test_select_related_objects_are_built_and_cached_on_their_parent() -> None:
    builder = ResultBuilder(
        BOOK_AND_AUTHOR, NO_CONVERTERS, NO_CONVERTERS, connection, Book, ModelState, [related_author()]
    )
    [book] = builder.instances([(7, "Dune", 1, 1, "Frank Herbert")], "default")
    assert (book.id, book.title, book.author_id) == (7, "Dune", 1)
    author_field: Any = Book._meta.get_field("author")
    assert author_field.is_cached(book)
    author = book.author
    assert type(author) is Author
    assert (author.id, author.name, author._state.db, author._state.adding) == (1, "Frank Herbert", "default", False)


def test_a_null_primary_key_means_no_related_object() -> None:
    cached: list[Any] = []
    author = RelatedModel(Author, 3, 5, 3, lambda book, value: cached.append(value), lambda author, book: None)
    builder = ResultBuilder(BOOK_AND_AUTHOR, NO_CONVERTERS, NO_CONVERTERS, connection, Book, ModelState, [author])
    builder.instances([(7, "Dune", None, None, None)], "default")
    assert cached == [None]


def test_the_parent_is_given_to_the_related_object_too() -> None:
    links: list[tuple[Any, Any]] = []
    builder = ResultBuilder(
        BOOK_AND_AUTHOR,
        NO_CONVERTERS,
        NO_CONVERTERS,
        connection,
        Book,
        ModelState,
        [related_author(lambda author, book: links.append((author, book)))],
    )
    [book] = builder.instances([(7, "Dune", 1, 1, "Frank Herbert")], "default")
    assert links == [(book.author, book)]


def test_related_columns_must_be_within_the_row() -> None:
    with pytest.raises(ValueError, match="first_column <= primary_key_column < end_column"):
        RelatedModel(Author, 3, 5, 5, print, print)
    author = RelatedModel(Author, 3, 9, 3, print, print)
    with pytest.raises(ValueError, match="within the row's columns"):
        ResultBuilder(BOOK_AND_AUTHOR, NO_CONVERTERS, NO_CONVERTERS, connection, Book, ModelState, [author])
