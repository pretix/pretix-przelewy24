import hashlib
import json
import logging
import requests
import uuid
from collections import OrderedDict
from decimal import Decimal
from django import forms
from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest
from django.template.loader import get_template
from django.utils.timezone import now
from django.utils.translation import get_language, gettext_lazy as _
from i18nfield.forms import I18nFormField, I18nTextInput
from i18nfield.strings import LazyI18nString
from pretix.base.decimal import round_decimal
from pretix.base.forms import SecretKeySettingsField
from pretix.base.forms.questions import guess_country_from_request
from pretix.base.models import InvoiceAddress, Order, OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.multidomain.urlreverse import build_absolute_uri
from urllib.parse import urljoin

logger = logging.getLogger(__name__)
supported_languages = [
    "bg",
    "cs",
    "de",
    "en",
    "es",
    "fr",
    "hr",
    "hu",
    "it",
    "nl",
    "pl",
    "pt",
    "se",
    "sk",
    "ro",
]


class Przelewy24(BasePaymentProvider):
    identifier = "przelewy24"
    verbose_name = _("Przelewy24")

    @property
    def public_name(self) -> str:
        return (
            self.settings.get("public_name", as_type=LazyI18nString)
            or self.verbose_name
        )

    @property
    def settings_form_fields(self):
        fields = [
            (
                "endpoint",
                forms.ChoiceField(
                    label=_("Environment"),
                    choices=[
                        ("production", _("Production")),
                        ("sandbox", _("Sandbox")),
                    ],
                ),
            ),
            (
                "merchant_id",
                forms.IntegerField(
                    label=_("Merchant ID"),
                ),
            ),
            (
                "pos_id",
                forms.IntegerField(
                    label=_("Shop ID"),
                    help_text=_("Defaults to merchant ID"),
                    required=False,
                ),
            ),
            (
                "api_key",
                SecretKeySettingsField(
                    label=_("API key"),
                    help_text='"Klucz do raportów"',  # untranslated in Przelewy24 web interface
                ),
            ),
            (
                "crc_key",
                SecretKeySettingsField(
                    label=_("CRC key"),
                ),
            ),
            (
                "public_name",
                I18nFormField(
                    label=_("Payment method name"),
                    widget=I18nTextInput,
                ),
            ),
        ]
        d = OrderedDict(fields + list(super().settings_form_fields.items()))
        del d["_invoice_text"]
        d.move_to_end("_enabled", last=False)
        return d

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        return True

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return True

    def checkout_prepare(self, request: HttpRequest, cart):
        return True

    def payment_prepare(self, request, payment):
        return True

    def payment_is_valid_session(self, request: HttpRequest) -> bool:
        return True

    def checkout_confirm_render(self, request) -> str:
        template = get_template("pretix_przelewy24/checkout_confirm_render.html")
        ctx = {
            "request": request,
        }
        return template.render(ctx)

    @property
    def api_url(self):
        if self.settings.endpoint == "production":
            return "https://secure.przelewy24.pl/api/v1/"
        else:
            return "https://sandbox.przelewy24.pl/api/v1/"

    @property
    def language(self):
        active = get_language()
        if active in supported_languages:
            return active
        if active[:2] in supported_languages:
            return active[:2]
        return "en"

    def _int_to_decimal(self, cents):
        places = settings.CURRENCY_PLACES.get(self.event.currency, 2)
        return round_decimal(float(cents) / (10**places), self.event.currency)

    def _decimal_to_int(self, amount):
        places = settings.CURRENCY_PLACES.get(self.event.currency, 2)
        return int(amount * 10**places)

    @property
    def _auth(self):
        return self.settings.pos_id or self.settings.merchant_id, self.settings.api_key

    def _get_payment_methods(self, currency, total):
        r = requests.get(
            urljoin(
                self.api_url,
                f"payment/methods/{self.language}?currency={currency}&total={self._decimal_to_int(total)}",
            ),
            auth=self._auth,
            timeout=3,
        )
        r.raise_for_status()
        return r.json()["data"]

    def payment_form_render(
        self, request: HttpRequest, total: Decimal, order: Order = None
    ) -> str:
        template = get_template("pretix_przelewy24/checkout_payment_form.html")

        try:
            payment_methods = cache.get_or_set(
                f"przelewy21_methods_{self.event.currency}_{total}",
                lambda: self._get_payment_methods(self.event.currency, total),
                timeout=3600 * 8,
            )
            payment_methods = sorted(
                [m for m in payment_methods if m["status"]], key=lambda m: m["name"]
            )
        except requests.RequestException:
            payment_methods = None

        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "payment_methods": payment_methods,
        }
        return template.render(ctx)

    def test_mode_message(self) -> str:
        if self.settings.endpoint == "sandbox":
            return _("Przelewy24 is operating in test mode.")

    def _sign_payment(self, payload):
        sign_payload = {
            "sessionId": payload["sessionId"],
            "merchantId": int(payload["merchantId"]),
            "amount": payload["amount"],
            "currency": payload["currency"],
            "crc": self.settings.crc_key,
        }
        encoded = json.dumps(
            sign_payload, ensure_ascii=False, separators=(",", ":")
        ).encode()
        payload["sign"] = hashlib.sha384(encoded).hexdigest()
        return payload

    def execute_payment(self, request: HttpRequest, payment: OrderPayment) -> str:
        request.session["payment_przelewy24_order_secret"] = payment.order.secret

        try:
            country = payment.order.invoice_address.country
        except InvoiceAddress.DoesNotExist:
            country = guess_country_from_request(request, self.event)

        r = None
        try:
            r = requests.post(
                urljoin(self.api_url, "transaction/register"),
                auth=self._auth,
                json=self._sign_payment(
                    {
                        "merchantId": int(self.settings.merchant_id),
                        "posId": self.settings.pos_id or self.settings.merchant_id,
                        "sessionId": payment.full_id,
                        "amount": self._decimal_to_int(payment.amount),
                        "currency": self.event.currency,
                        "description": f"{payment.order.code} - {self.event.name}"[
                            :1024
                        ],
                        "email": payment.order.email,
                        # "client": "string",
                        # "address": "string",
                        # "zip": "string",
                        # "city": "string",
                        "country": str(country) or "PL",
                        # "phone": "string",
                        "language": self.language,
                        "urlReturn": build_absolute_uri(
                            self.event,
                            "plugins:pretix_przelewy24:return",
                            kwargs={
                                "order": payment.order.code,
                                "payment": payment.pk,
                                "hash": payment.order.tagged_secret(
                                    "plugins:pretix_przelewy24:return"
                                ),
                            },
                        ),
                        "urlStatus": build_absolute_uri(
                            self.event,
                            "plugins:pretix_przelewy24:callback",
                            kwargs={
                                "order": payment.order.code,
                                "payment": payment.pk,
                                "hash": payment.order.tagged_secret(
                                    "plugins:pretix_przelewy24:status"
                                ),
                            },
                        ),
                        "timeLimit": min(
                            99,
                            int((payment.order.expires - now()).total_seconds() // 60),
                        ),
                        "waitForResult": False,
                        "regulationAccept": False,
                        "transferLabel": payment.full_id[:20],
                        "encoding": "UTF-8",
                    }
                ),
            )
            r.raise_for_status()
            token = r.json()["data"]["token"]

            payment.info_data = {"token": token}
            payment.save(update_fields=["info"])

            return urljoin(self.api_url, f"/trnRequest/{token}")
        except (requests.RequestException, ValueError) as e:
            logger.exception("Failed to contact Przelewy24")
            payment.fail(
                info={
                    "error": True,
                    "message": str(e),
                    "response": r.text if r is not None else "",
                },
                log_data={
                    "response": r.text if r is not None else "",
                    "message": str(e),
                },
            )

            raise PaymentException(
                _("We were unable to contact Przelewy24. Please try again later.")
            )

    def _sign_verification(self, payload):
        sign_payload = {
            "sessionId": payload["sessionId"],
            "orderId": payload["orderId"],
            "amount": payload["amount"],
            "currency": payload["currency"],
            "crc": self.settings.crc_key,
        }
        encoded = json.dumps(
            sign_payload, ensure_ascii=False, separators=(",", ":")
        ).encode()
        payload["sign"] = hashlib.sha384(encoded).hexdigest()
        return payload

    def _verify_transaction(self, payment: OrderPayment):
        if "orderId" not in payment.info_data:
            raise PaymentException("Invalid state of payment.")

        order_id = payment.info_data["orderId"]

        r = None
        try:
            r = requests.put(
                urljoin(self.api_url, "transaction/verify"),
                auth=self._auth,
                json=self._sign_verification(
                    {
                        "merchantId": int(self.settings.merchant_id),
                        "posId": self.settings.pos_id or self.settings.merchant_id,
                        "sessionId": payment.full_id,
                        "amount": self._decimal_to_int(payment.amount),
                        "currency": self.event.currency,
                        "orderId": int(order_id),
                    }
                ),
            )
            r.raise_for_status()
            if r.json().get("data", {}).get("status", "") == "success":
                payment.confirm()
            else:
                payment.fail(log_data={"response": r.json()})
        except (requests.RequestException, ValueError) as e:
            payment.info_data = {
                **payment.info_data,
                "verification_error": str(e),
                "verification_response": r.text if r is not None else None,
            }
            payment.save(update_fields=["info"])
            raise PaymentException(
                _("We were unable to contact Przelewy24. Please try again later.")
            )

    def _check_transaction(self, payment: OrderPayment):
        r = None
        try:
            r = requests.get(
                urljoin(self.api_url, f"transaction/by/sessionId/{payment.full_id}"),
                auth=self._auth,
            )
            r.raise_for_status()
            payment.fail(
                info={
                    **payment.info_data,
                    **r.json()["data"],
                }
            )

            # Seems like we can't detect a pending payment properly :(
            if r.json()["data"]["status"] != 1:
                raise PaymentException(_("No successful payment was detected."))
        except (requests.RequestException, ValueError) as e:
            payment.info_data = {
                **payment.info_data,
                "check_error": str(e),
                "check_response": r.text if r is not None else None,
            }
            payment.save(update_fields=["info"])
            raise PaymentException(
                _("We were unable to contact Przelewy24. Please try again later.")
            )

    def shred_payment_info(self, obj: OrderPayment):
        if not obj.info:
            return
        whitelist = {
            "accountMD5",
            "amount",
            "batchId",
            "currency",
            "date",
            "dateOfTransaction",
            "description",
            "fee",
            "orderId",
            "paymentMethod",
            "sessionId",
            "statement",
            "status",
            "token",
            "check_error",
            "check_response",
            "error",
            "message",
            "response",
            "verification_error",
            "verification_response",
        }
        d = obj.info_data
        d = {k: ("█" if v and k not in whitelist else v) for k, v in d.items()}

        d["_shredded"] = True
        obj.info = json.dumps(d)
        obj.save(update_fields=["info"])

    def payment_control_render(self, request, payment) -> str:
        if payment.info:
            payment_info = payment.info_data
        else:
            payment_info = None
        template = get_template("pretix_przelewy24/control.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "payment_info": payment_info,
            "payment": payment,
            "provider": self,
        }
        return template.render(ctx)

    def execute_refund(self, refund: OrderRefund):
        if "orderId" not in refund.payment.info_data:
            raise PaymentException("Invalid state of payment.")

        r = None
        try:
            r = requests.post(
                urljoin(self.api_url, "transaction/refund"),
                auth=self._auth,
                json={
                    "requestId": refund.full_id,
                    "refunds": [
                        {
                            "orderId": refund.payment.info_data["orderId"],
                            "sessionId": refund.payment.full_id,
                            "amount": self._decimal_to_int(refund.amount),
                        },
                    ],
                    "refundsUuid": str(uuid.uuid4()),
                    "urlStatus": build_absolute_uri(
                        self.event,
                        "plugins:pretix_przelewy24:refundcallback",
                        kwargs={
                            "order": refund.order.code,
                            "refund": refund.pk,
                            "hash": refund.order.tagged_secret(
                                "plugins:pretix_przelewy24:refundstatus"
                            ),
                        },
                    ),
                },
            )
            r.raise_for_status()
            data = r.json().get("data", [])[0]

            refund.info_data = data

            if data.get("status", False) is True:
                refund.state = OrderRefund.REFUND_STATE_TRANSIT
            else:
                refund.state = OrderRefund.REFUND_STATE_FAILED
            refund.save(update_fields=["info", "state"])
        except (requests.RequestException, ValueError) as e:
            refund.info_data = {
                "error": str(e),
                "response": r.text if r is not None else None,
            }
            refund.state = OrderRefund.REFUND_STATE_FAILED
            refund.save(update_fields=["info"])
            raise PaymentException(
                _("We were unable to contact Przelewy24. Please try again later.")
            )

    def api_payment_details(self, payment: OrderPayment):
        return {
            "order_id": payment.info_data.get("orderId"),
            "payment_method": payment.info_data.get("paymentMethod"),
        }
