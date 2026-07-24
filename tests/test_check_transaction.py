"""Tests for the race-condition fix in Przelewy24._check_transaction.

See FIX_PLAN.md: the return view (_check_transaction) must no longer fail a
payment unconditionally. It persists the fetched transaction data, verifies
paid/verified transactions, and leaves everything else pending for the
server-to-server callback to confirm.
"""
import json
from unittest import mock

import pytest
from django_scopes import scopes_disabled

from pretix.base.models import OrderPayment
from pretix.base.payment import PaymentException

from pretix_przelewy24.payment import Przelewy24


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def make_provider(event):
    return Przelewy24(event)


@pytest.mark.django_db
def test_status_zero_keeps_payment_pending(event, payment):
    """status 0 (no payment): raise, but do not fail the payment."""
    provider = make_provider(event)
    with scopes_disabled(), mock.patch(
        "pretix_przelewy24.payment.requests.get",
        return_value=FakeResponse({"data": {"status": 0}}),
    ):
        with pytest.raises(PaymentException):
            provider._check_transaction(payment)

    payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_PENDING


@pytest.mark.django_db
def test_status_one_verifies_and_confirms(event, payment):
    """status 1 (paid): run verification, which confirms on success."""
    provider = make_provider(event)
    with scopes_disabled(), mock.patch(
        "pretix_przelewy24.payment.requests.get",
        return_value=FakeResponse({"data": {"status": 1, "orderId": 999}}),
    ), mock.patch(
        "pretix_przelewy24.payment.requests.put",
        return_value=FakeResponse({"data": {"status": "success"}}),
    ):
        provider._check_transaction(payment)

    payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED


@pytest.mark.django_db
def test_status_two_on_confirmed_payment_is_noop(event, payment):
    """status 2 on an already-confirmed payment: no verify call, no error."""
    with scopes_disabled():
        payment.state = OrderPayment.PAYMENT_STATE_CONFIRMED
        payment.save()

    provider = make_provider(event)
    with scopes_disabled(), mock.patch(
        "pretix_przelewy24.payment.requests.get",
        return_value=FakeResponse({"data": {"status": 2, "orderId": 999}}),
    ), mock.patch("pretix_przelewy24.payment.requests.put") as put_mock:
        provider._check_transaction(payment)

    put_mock.assert_not_called()
    payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED


@pytest.mark.django_db
def test_verify_non_success_fails_payment(event, payment):
    """status 1 but verify answers non-success: payment is failed (genuine)."""
    provider = make_provider(event)
    with scopes_disabled(), mock.patch(
        "pretix_przelewy24.payment.requests.get",
        return_value=FakeResponse({"data": {"status": 1, "orderId": 999}}),
    ), mock.patch(
        "pretix_przelewy24.payment.requests.put",
        return_value=FakeResponse({"data": {"status": "rejected"}}),
    ):
        provider._check_transaction(payment)

    payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_FAILED


@pytest.mark.django_db
def test_fetched_data_is_persisted(event, payment):
    """The by-sessionId payload is merged into info_data even on failure path."""
    provider = make_provider(event)
    with scopes_disabled(), mock.patch(
        "pretix_przelewy24.payment.requests.get",
        return_value=FakeResponse({"data": {"status": 0, "batchId": 4242}}),
    ):
        with pytest.raises(PaymentException):
            provider._check_transaction(payment)

    payment.refresh_from_db()
    assert payment.info_data.get("batchId") == 4242
