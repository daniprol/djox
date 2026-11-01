"""Authors and their books."""

from django.db import models


class Author(models.Model):
    name = models.CharField(max_length=100)


class Book(models.Model):
    title = models.CharField(max_length=200)
    pages = models.IntegerField()
    price = models.DecimalField(max_digits=6, decimal_places=2)
    published = models.DateTimeField()
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")
