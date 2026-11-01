"""`DJOX_MODELS`: which models `"djox"` in `INSTALLED_APPS` switches to djox."""

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured

from djox.install import is_chosen
from tests.e2e.shelf.models import Box, Shelf


def test_all_chooses_every_model() -> None:
    assert is_chosen(User, "__all__")


def test_an_app_label_chooses_the_apps_models() -> None:
    assert is_chosen(Shelf, ["shelf"])
    assert not is_chosen(User, ["shelf"])


def test_a_model_label_chooses_that_model_only() -> None:
    assert is_chosen(Shelf, ["shelf.Shelf"])
    assert not is_chosen(Box, ["shelf.Shelf"])


def test_an_empty_list_chooses_nothing() -> None:
    assert not is_chosen(Shelf, [])


def test_anything_else_is_a_configuration_error() -> None:
    with pytest.raises(ImproperlyConfigured, match="DJOX_MODELS"):
        is_chosen(Shelf, "shelf")
