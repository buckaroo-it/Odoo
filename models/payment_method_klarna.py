# Part of Odoo. See LICENSE file for full copyright and licensing details.

import re

from buckaroo.services.payment_service import PaymentService

from odoo import _, fields, models
from odoo.exceptions import ValidationError

from ..helpers.articles import get_order_articles
from ..helpers.customer import (
    pop_session_value,
    resolve_bnpl_customer_data,
    sanitize_phone,
    split_house_number,
)


class PaymentMethodKlarna(models.Model):
    _inherit = "payment.method"

    @staticmethod
    def _format_klarna_articles(articles):
        return [
            {
                "articleNumber": a["identifier"],
                # Klarna's articleTitle rejects multi-line descriptions.
                "articleTitle": re.sub(r"\s+", " ", (a["description"] or "")).strip(),
                "articleQuantity": str(int(a["quantity"])),
                "articlePrice": a["unit_price_incl"],
                "articleVat": a["vat_percentage"],
            }
            for a in articles
            if a["unit_price_incl"] > 0
        ]

    def _buckaroo_get_payment_action(self):
        """Klarna MOR is always Reserve→Pay; never immediate capture."""
        self.ensure_one()
        if self.code == "klarna":
            return "authorize"
        return super()._buckaroo_get_payment_action()

    def _buckaroo_create_payment(self, transaction, client):
        if self.code != "klarna":
            return super()._buckaroo_create_payment(transaction, client)

        params = self._buckaroo_get_payment_params(transaction)
        builder = PaymentService(client).create_payment(
            self._buckaroo_get_sdk_service_name(),
            params,
        )

        articles = get_order_articles(transaction)
        if articles:
            builder.add_parameter("article", self._format_klarna_articles(articles))

        billing_data, shipping_data, same_address = resolve_bnpl_customer_data(transaction)

        # ``int(gender)`` is unguarded — both write paths (controller
        # validation + Selection ``('1', '2')``) constrain the value.
        gender = (
            pop_session_value("buckaroo_klarna_gender")
            or transaction.partner_id.buckaroo_klarna_gender
            or ""
        )
        if not gender:
            raise ValidationError(_("Please select your gender to proceed with Klarna."))
        builder.add_parameter("gender", int(gender))
        if billing_data["country_code"]:
            builder.add_parameter("operatingCountry", billing_data["country_code"])

        builder.add_parameter("shippingSameAsBilling", "true" if same_address else "false")

        self._add_klarna_address(builder, "Billing", billing_data)
        self._add_klarna_address(builder, "Shipping", shipping_data)

        return builder.reserve(validate=False)

    def _buckaroo_create_capture(self, transaction, client):
        """Klarna capture = Pay action with DataRequestKey from prior Reserve."""
        if self.code != "klarna":
            return super()._buckaroo_create_capture(transaction, client)
        params, data_request_key = self._buckaroo_get_post_authorize_params(transaction)
        builder = PaymentService(client).create_payment(
            self._buckaroo_get_sdk_service_name(),
            params,
        )
        builder.add_parameter("dataRequestKey", data_request_key)
        return self._buckaroo_klarna_normalize_idempotent(builder.pay(), "Captured")

    def _buckaroo_create_void(self, transaction, client):
        """Klarna void = CancelReservation with OriginalTransactionKey."""
        if self.code != "klarna":
            return super()._buckaroo_create_void(transaction, client)
        params, original_key = self._buckaroo_get_post_authorize_params(transaction)
        builder = PaymentService(client).create_payment(
            self._buckaroo_get_sdk_service_name(),
            params,
        )
        response = builder.cancelReservation(original_transaction_key=original_key)
        return self._buckaroo_klarna_normalize_idempotent(response, ("Cancelled", "Canceled"))

    @staticmethod
    def _add_klarna_address(builder, prefix, data):
        number, suffix = split_house_number(data["house_number"])
        builder.add_parameter(prefix + "Street", data["street_name"])
        builder.add_parameter(prefix + "HouseNumber", number)
        builder.add_parameter(prefix + "HouseNumberSuffix", suffix)
        builder.add_parameter(prefix + "PostalCode", data["postal_code"])
        builder.add_parameter(prefix + "City", data["city"])
        builder.add_parameter(prefix + "Country", data["country_code"])
        builder.add_parameter(
            prefix + "CellPhoneNumber",
            sanitize_phone(data["phone"]),
        )
        builder.add_parameter(prefix + "Email", data["email"])

    @staticmethod
    def _buckaroo_klarna_normalize_idempotent(response, target_statuses):
        """Promote Klarna's "already in target state" 490 to success.

        Odoo's ``service.model.retrying`` re-fires the request handler
        on serialization failure; the second SDK call hits Klarna's
        ``InvalidOrderStatus`` 490 even though the first one succeeded.
        Casing varies by region, so accept a tuple of Klarna-side states.
        """
        if isinstance(target_statuses, str):
            target_statuses = (target_statuses,)
        if not response.status or not response.status.code:
            return response
        if response.status.code.code != 490:
            return response
        sub = response.status.sub_code
        message = (sub.description if sub else "") or ""
        if "InvalidOrderStatus" not in message:
            return response
        matched = next(
            (s for s in target_statuses if "Current status: %s" % s in message),
            None,
        )
        if not matched:
            return response
        response.status.code.code = 190
        response.status.code.description = "Idempotent: already %s" % matched
        return response


class ResPartner(models.Model):
    _inherit = "res.partner"

    buckaroo_klarna_gender = fields.Selection(
        selection=[("1", "He/Him"), ("2", "She/Her")],
        string="Klarna Salutation",
        help="Saved from the last Klarna checkout to prefill on next order.",
    )
