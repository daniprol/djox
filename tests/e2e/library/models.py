"""A small Django app for the tests: authors (a `djox.Model`) and their books
(with `djox.Manager`), plus editions, publishers and biographies for
`select_related()` and `prefetch_related()`."""

from django.db import models

import djox


class Author(djox.Model):
    name = models.CharField(max_length=100)


class Book(models.Model):
    title = models.CharField(max_length=200)
    pages = models.IntegerField()
    price = models.DecimalField(max_digits=6, decimal_places=2)
    published = models.DateTimeField()
    in_print = models.BooleanField(default=True)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")

    objects = djox.Manager()


class Publisher(models.Model):
    """For `select_related()` through a nullable foreign key (`Edition.publisher`)."""

    name = models.CharField(max_length=100)

    objects = djox.Manager()


class Edition(models.Model):
    """An edition of a book: `select_related("book__author", "publisher")` reaches
    two levels deep, and an edition may have no publisher."""

    # `DO_NOTHING`: deleting books stays one `DELETE` (a "fast delete", which
    # the fast lane's tests check), since no edition needs to know.
    book = models.ForeignKey(Book, on_delete=models.DO_NOTHING, related_name="editions")
    publisher = models.ForeignKey(Publisher, on_delete=models.SET_NULL, null=True, related_name="editions")
    number = models.IntegerField()

    objects = djox.Manager()


class Biography(models.Model):
    """At most one per author: `Author.objects.select_related("biography")`
    follows a one-to-one backwards."""

    author = models.OneToOneField(Author, on_delete=models.CASCADE, related_name="biography")
    text = models.TextField()

    objects = djox.Manager()
