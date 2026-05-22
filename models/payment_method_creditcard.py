# Part of Odoo. See LICENSE file for full copyright and licensing details.

from buckaroo.services.payment_service import PaymentService

from odoo import _, fields, models
from odoo.exceptions import ValidationError


class PaymentMethodCreditCard(models.Model):
    _inherit = "payment.method"

    buckaroo_official_creditcard_authorize = fields.Selection(
        string="Payment Action",
        selection=[("pay", "Pay"), ("authorize", "Authorize")],
        default="pay",
        help="Choose whether to capture the payment immediately ('Pay') or "
        "only authorize it for later manual capture ('Authorize').",
    )
    buckaroo_official_creditcard_method = fields.Selection(
        string="Credit Card Method",
        selection=[("redirect", "Redirect"), ("inline", "Inline (Hosted Fields)")],
        default="redirect",
        help="Choose how credit card payments are handled. "
        "'Redirect' sends the customer to Buckaroo's hosted payment page. "
        "'Inline (Hosted Fields)' embeds card fields directly on your checkout page.",
    )
    buckaroo_official_hosted_fields_client_id = fields.Char(
        string="Hosted Fields Client ID",
        help="OAuth Client ID for Hosted Fields. "
        "Find this in Buckaroo Plaza > Settings > Token Registration.",
        copy=False,
    )
    buckaroo_official_hosted_fields_client_secret = fields.Char(
        string="Hosted Fields Client Secret",
        help="OAuth Client Secret for Hosted Fields. "
        "Find this in Buckaroo Plaza > Settings > Token Registration.",
        copy=False,
        groups="base.group_system",
    )

    def _buckaroo_get_payment_action(self):
        """Return 'authorize' for creditcard when configured; else delegate."""
        self.ensure_one()
        if self.code == "buckaroo_creditcard" and self.buckaroo_official_creditcard_authorize == "authorize":
            return "authorize"
        return super()._buckaroo_get_payment_action()

    def _buckaroo_create_payment(self, transaction, client):
        """Credit card flow: 4-way routing on redirect/inline x pay/authorize.

        Flows:
        1. Redirect + pay       -> .pay()
        2. Redirect + authorize -> .authorize()
        3. Inline + pay         -> .payWithToken() with HF session
        4. Inline + authorize   -> .authorizeWithToken() with HF session
        """
        self.ensure_one()
        if self.code != "buckaroo_creditcard":
            return super()._buckaroo_create_payment(transaction, client)

        from odoo.http import request  # noqa: PLC0415

        hf_session_id = request.session.pop("buckaroo_hf_session_id", None) if request else None
        hf_service = request.session.pop("buckaroo_hf_service", None) if request else None
        authorize = self.buckaroo_official_creditcard_authorize

        params = self._buckaroo_get_payment_params(transaction)
        if hf_session_id and hf_service:
            params["brand"] = hf_service

        builder = PaymentService(client).create_payment(
            self.buckaroo_official_sdk_service_name,
            params,
        )

        if hf_session_id:
            builder.add_parameter("SessionId", hf_session_id)
            if authorize == "authorize":
                return builder.authorizeWithToken()
            return builder.payWithToken()

        if authorize == "authorize":
            return builder.authorize()
        return builder.pay()

    def _buckaroo_get_refund_params(self, source_tx, refund_tx):
        """Add card brand to refund params for creditcard; delegate otherwise."""
        if self.code != "buckaroo_creditcard":
            return super()._buckaroo_get_refund_params(source_tx, refund_tx)
        params = super()._buckaroo_get_refund_params(source_tx, refund_tx)
        params["brand"] = self._buckaroo_require_card_brand(source_tx)
        return params

    def _buckaroo_get_post_authorize_params(self, transaction):
        """Add card brand to capture/void params for creditcard; delegate otherwise."""
        if self.code != "buckaroo_creditcard":
            return super()._buckaroo_get_post_authorize_params(transaction)
        params, original_key = super()._buckaroo_get_post_authorize_params(transaction)
        source_tx = transaction.source_transaction_id or transaction
        params["brand"] = self._buckaroo_require_card_brand(source_tx)
        return params, original_key

    def _buckaroo_require_card_brand(self, source_tx):
        # Brand is written only on the root (authorize/pay) tx; capture/void
        # and refund chained off a capture must walk up source_transaction_id.
        return self._buckaroo_require_service_code(source_tx, _(
            "Card brand unknown on the original transaction. The "
            "authorization may have been created before service code "
            "tracking was enabled."
        ))
