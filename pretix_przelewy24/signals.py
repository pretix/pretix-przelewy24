import logging
from django.dispatch import receiver
from pretix.base.signals import register_payment_providers

logger = logging.getLogger(__name__)


@receiver(register_payment_providers, dispatch_uid="payment_przelewy24")
def register_payment_provider(sender, **kwargs):
    from .payment import Przelewy24

    return Przelewy24
