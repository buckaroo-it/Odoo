# Part of Odoo. See LICENSE file for full copyright and licensing details.

import base64

from buckaroo.services.payment_service import PaymentService

from odoo import _, fields, models
from odoo.exceptions import ValidationError

from ..helpers.customer import pop_session_value
from ..utils import const


class PaymentMethodGooglepay(models.Model):
    _inherit = 'payment.method'

    buckaroo_official_googlepay_merchant_guid = fields.Char(
        string="Buckaroo Merchant GUID",
        help="The Merchant GUID issued by Buckaroo for Google Pay. "
             "Find this in Buckaroo Plaza under your Google Pay configuration.",
        copy=False,
    )
    buckaroo_official_googlepay_google_merchant_id = fields.Char(
        string="Google Merchant ID",
        help="The merchant identifier issued by Google in the Google Pay & "
             "Wallet Console. Required for production Google Pay sessions.",
        copy=False,
    )

    def _get_compatible_payment_methods(
        self, provider_ids, partner_id, currency_id=None, force_tokenization=False,
        is_express_checkout=False, report=None, **kwargs
    ):
        payment_methods = super()._get_compatible_payment_methods(
            provider_ids, partner_id, currency_id=currency_id,
            force_tokenization=force_tokenization,
            is_express_checkout=is_express_checkout, report=report, **kwargs
        )
        return payment_methods.filtered(
            lambda pm: pm.code != 'googlepay'
            or pm._buckaroo_googlepay_is_configured()
        )

    def _buckaroo_googlepay_is_configured(self):
        """`merchant_guid` always required; `google_merchant_id` only in PROD."""
        self.ensure_one()
        if not self.buckaroo_official_googlepay_merchant_guid:
            return False
        prod_provider = self.provider_ids.filtered(
            lambda p: p.code == const.PROVIDER_CODE and p.state == 'enabled'
        )
        if prod_provider and not self.buckaroo_official_googlepay_google_merchant_id:
            return False
        return True

    def _buckaroo_create_payment(self, transaction, client):
        if self.code != 'googlepay':
            return super()._buckaroo_create_payment(transaction, client)

        token = pop_session_value('buckaroo_googlepay_token')
        customer_name = pop_session_value('buckaroo_googlepay_customer_name')
        if not token or not customer_name:
            raise ValidationError(_(
                "Google Pay payment token is missing. "
                "Please retry the Google Pay flow."
            ))

        # Buckaroo's API rejects the raw Google Pay token; it must be base64.
        encoded_token = base64.b64encode(token.encode('utf-8')).decode('ascii')

        params = self._buckaroo_get_payment_params(transaction)
        builder = PaymentService(client).create_payment(
            self._buckaroo_get_sdk_service_name(), params,
        )
        builder.add_parameter('PaymentData', encoded_token)
        builder.add_parameter('CustomerCardName', customer_name)
        return builder.pay()
