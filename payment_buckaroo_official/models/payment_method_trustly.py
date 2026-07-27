# Part of Odoo. See LICENSE file for full copyright and licensing details.

from buckaroo.services.payment_service import PaymentService

from odoo import models

from ..helpers.customer import get_customer_data


class PaymentMethodTrustly(models.Model):
    _inherit = "payment.method"

    def _buckaroo_create_payment(self, transaction, client):
        if self.code != "buckaroo_trustly":
            return super()._buckaroo_create_payment(transaction, client)

        params = self._buckaroo_get_payment_params(transaction)
        builder = PaymentService(client).create_payment(
            self.buckaroo_official_sdk_service_name,
            params,
        )

        # Buckaroo requires both name parts; a single-word partner name leaves
        # last_name empty, so fall back to the first name.
        customer = get_customer_data(transaction.partner_id)
        builder.add_parameter("consumeremail", customer["email"])
        builder.add_parameter("customerFirstName", customer["first_name"])
        builder.add_parameter("customerLastName", customer["last_name"] or customer["first_name"])
        builder.add_parameter("customerCountryCode", customer["country_code"])

        return builder.pay()
