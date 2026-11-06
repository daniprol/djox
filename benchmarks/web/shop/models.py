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
    read_count = models.IntegerField(default=0)  # how many times the book was read: `F("read_count") + 1`


class Read(models.Model):
    """A log of reads of a book: one row per read, written in the same transaction as the counter."""

    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="reads")
    read_at = models.DateTimeField()
