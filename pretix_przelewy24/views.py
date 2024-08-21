import json
from django.contrib import messages
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from pretix.base.models import Order, OrderPayment
from pretix.base.payment import PaymentException
from pretix.helpers import OF_SELF
from pretix.multidomain.urlreverse import eventreverse


class Przelewy24OrderView:
    tag = None

    def dispatch(self, request, *args, **kwargs):
        try:
            self.order = request.event.orders.get_with_secret_check(
                code=kwargs["order"], received_secret=kwargs["hash"], tag=self.tag
            )
        except Order.DoesNotExist:
            raise Http404("Order not found")
        return super().dispatch(request, *args, **kwargs)

    def _redirect_to_order(self):
        if (
            self.request.session.get("payment_przelewy24_order_secret")
            != self.order.secret
        ):
            messages.error(
                self.request,
                _(
                    "Sorry, there was an error in the payment process. Please check the link "
                    "in your emails to continue."
                ),
            )
            return redirect(eventreverse(self.request.event, "presale:event.index"))

        return redirect(
            eventreverse(
                self.request.event,
                "presale:event.order",
                kwargs={"order": self.order.code, "secret": self.order.secret},
            )
            + ("?paid=yes" if self.order.status == Order.STATUS_PAID else "")
        )


@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(csrf_exempt, "dispatch")
class ReturnView(Przelewy24OrderView, View):
    tag = "plugins:pretix_przelewy24:return"

    def post(self, request, *args, **kwargs):
        # Przelewy24 seems to do a post redirect
        return self.get(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        # We just wait in case of concurrent lock, then redirect or check for failure
        with transaction.atomic():
            payment = get_object_or_404(
                self.order.payments.select_for_update(of=OF_SELF),
                pk=self.kwargs["payment"],
                provider__startswith="przelewy24",
            )
            pp = payment.payment_provider

            try:
                pp._check_transaction(payment)
            except PaymentException as e:
                if payment.state != OrderPayment.PAYMENT_STATE_CONFIRMED:
                    messages.error(request, str(e))

            return self._redirect_to_order()


@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(csrf_exempt, "dispatch")
class CallbackView(Przelewy24OrderView, View):
    tag = "plugins:pretix_przelewy24:status"

    def post(self, request, *args, **kwargs):
        status_data = json.loads(request.body.decode("utf-8"))
        # We do not verify the CRC as we do not trust the stableness of their signatures, however the unique URL with
        # a custom tag ensures the request is really coming from Przelewy24.

        # We ignore everything that was sent and just query the transaction
        with transaction.atomic():
            payment = get_object_or_404(
                self.order.payments.select_for_update(of=OF_SELF),
                pk=self.kwargs["payment"],
                provider__startswith="przelewy24",
            )

            payment.info_data = {
                **payment.info_data,
                **status_data,
            }
            payment.save(update_fields=["info"])

            pp = payment.payment_provider

            try:
                pp._verify_transaction(payment)
            except PaymentException as e:
                return HttpResponse(str(e), status=400)

            return HttpResponse("OK", status=200)


@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(csrf_exempt, "dispatch")
class RefundCallbackView(Przelewy24OrderView, View):
    tag = "plugins:pretix_przelewy24:refundstatus"

    def post(self, request, *args, **kwargs):
        status_data = json.loads(request.body.decode("utf-8"))
        # We do not verify the CRC as we do not trust the stableness of their signatures, however the unique URL with
        # a custom tag ensures the request is really coming from Przelewy24.

        # We ignore everything that was sent and just query the transaction
        with transaction.atomic():
            refund = get_object_or_404(
                self.order.refunds.select_for_update(of=OF_SELF),
                pk=self.kwargs["refund"],
                provider__startswith="przelewy24",
            )

            refund.info_data = {
                **refund.info_data,
                **status_data,
            }
            refund.done()

            return HttpResponse("OK", status=200)
