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

    def _buckaroo_creditcard_redirect_brands(self):
        """Active card brands offered by the redirect checkout selector.

        Buckaroo has no generic ``creditcard`` service - every Pay/Authorize
        names a specific brand, so the customer chooses one in checkout. Several
        brand records share one SDK service (V PAY, Carte Bancaire and others all
        run on ``Visa``), so dedupe by service while keeping the first display
        name. Returns ``[{"service": ..., "label": ...}, ...]``.
        """
        self.ensure_one()
        brands = {}
        for brand in self.brand_ids.filtered(
            lambda b: b.active and b.buckaroo_official_sdk_service_name
        ):
            brands.setdefault(brand.buckaroo_official_sdk_service_name, brand.name)
        return [{"service": service, "label": label} for service, label in brands.items()]

    def _buckaroo_create_payment(self, transaction, client):
        """Credit card flow: routing on redirect/inline x pay/authorize.

        The SDK service name is always ``creditcard``; the concrete card brand
        (from the Hosted Fields session inline, or the checkout dropdown on
        redirect) is passed in ``params["brand"]``. Buckaroo then redirects to
        that brand's hosted card-entry page.
        """
        self.ensure_one()
        if self.code != "buckaroo_creditcard":
            return super()._buckaroo_create_payment(transaction, client)

        from odoo.http import request  # noqa: PLC0415

        hf_session_id = request.session.pop("buckaroo_hf_session_id", None) if request else None
        hf_service = request.session.pop("buckaroo_hf_service", None) if request else None
        cc_brand = request.session.pop("buckaroo_cc_brand", None) if request else None
        authorize = self.buckaroo_official_creditcard_authorize

        params = self._buckaroo_get_payment_params(transaction)

        if hf_session_id:
            if hf_service:
                params["brand"] = hf_service
                # Persist the brand now so a later refund/capture has it even if
                # the Buckaroo push omits the service code.
                transaction.buckaroo_official_service_code = hf_service
            builder = PaymentService(client).create_payment(
                self.buckaroo_official_sdk_service_name,
                params,
            )
            builder.add_parameter("SessionId", hf_session_id)
            if authorize == "authorize":
                return builder.authorizeWithToken()
            return builder.payWithToken()

        allowed = {
            b.buckaroo_official_sdk_service_name
            for b in self.brand_ids.filtered(
                lambda b: b.active and b.buckaroo_official_sdk_service_name
            )
        }
        if cc_brand not in allowed:
            raise ValidationError(_("Please select a valid card type."))
        params["brand"] = cc_brand
        transaction.buckaroo_official_service_code = cc_brand
        builder = PaymentService(client).create_payment(
            self.buckaroo_official_sdk_service_name,
            params,
        )
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
