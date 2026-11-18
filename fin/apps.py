from django.apps import AppConfig


class FinConfig(AppConfig):
    name = "fin"
    verbose_name = "Financeiro"

    def ready(self):
        from . import signals  # noqa: F401
