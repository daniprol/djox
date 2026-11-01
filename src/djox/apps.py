"""djox as a Django app: `"djox"` in `INSTALLED_APPS` switches existing models to djox.

See `djox.install` for what it changes, and the `DJOX_MODELS` setting.
"""

from django.apps import AppConfig


class DjoxConfig(AppConfig):
    """The `"djox"` app. It has no models with tables of its own."""

    name = "djox"
    verbose_name = "djox"

    def ready(self) -> None:
        """Called by Django once every app's models are loaded."""
        from djox.install import use_djox_for_chosen_models

        use_djox_for_chosen_models()
