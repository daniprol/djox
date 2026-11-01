"""A small Django app for the tests: authors and their books, with `djox.Manager`."""

from django.db import models

import djox


class Author(models.Model):
    name = models.CharField(max_length=100)

    objects = djox.Manager()


class Book(models.Model):
    title = models.CharField(max_length=200)
    pages = models.IntegerField()
    price = models.DecimalField(max_digits=6, decimal_places=2)
    published = models.DateTimeField()
    in_print = models.BooleanField(default=True)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")

    objects = djox.Manager()
