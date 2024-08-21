from django.utils.translation import gettext_lazy

from . import __version__

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")


class PluginApp(PluginConfig):
    default = True
    name = "pretix_przelewy24"
    verbose_name = "Przelewy24"

    class PretixPluginMeta:
        name = gettext_lazy("Przelewy24")
        author = "pretix team"
        description = gettext_lazy(
            "Accept payments through Przelewy24, a Polish payment facilitator."
        )
        visible = True
        picture = "pretix_przelewy24/przelewy24_logo.svg"
        version = __version__
        category = "PAYMENT"
        compatibility = "pretix>=2024.7.0"

    def ready(self):
        from . import signals  # NOQA
