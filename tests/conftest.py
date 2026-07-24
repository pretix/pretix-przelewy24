# put your pytest fixtures here
from datetime import timedelta

import pytest
from django.utils.timezone import now
from django_scopes import scopes_disabled

from pretix.base.models import Event, Order, OrderPayment, Organizer


@pytest.fixture
def event():
    with scopes_disabled():
        o = Organizer.objects.create(name="Dummy", slug="dummy")
        event = Event.objects.create(
            organizer=o,
            name="Dummy",
            slug="dummy",
            date_from=now(),
            plugins="pretix_przelewy24",
            currency="PLN",
        )
        event.settings.set("payment_przelewy24_merchant_id", "12345")
        event.settings.set("payment_przelewy24_pos_id", "12345")
        event.settings.set("payment_przelewy24_api_key", "apikey")
        event.settings.set("payment_przelewy24_crc_key", "crckey")
        event.settings.set("payment_przelewy24_endpoint", "sandbox")
        yield event


@pytest.fixture
def order(event):
    with scopes_disabled():
        return Order.objects.create(
            code="FOO",
            event=event,
            email="dummy@dummy.test",
            status=Order.STATUS_PENDING,
            datetime=now(),
            expires=now() + timedelta(days=10),
            total=23,
            sales_channel=event.organizer.sales_channels.get(identifier="web"),
        )


@pytest.fixture
def payment(order):
    with scopes_disabled():
        return OrderPayment.objects.create(
            order=order,
            amount=order.total,
            provider="przelewy24",
            state=OrderPayment.PAYMENT_STATE_PENDING,
            info='{"orderId": 999, "token": "tok"}',
        )
