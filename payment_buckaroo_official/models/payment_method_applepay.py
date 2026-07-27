# Part of Odoo. See LICENSE file for full copyright and licensing details.

import base64

from buckaroo.services.payment_service import PaymentService

from odoo import _, fields, models
from odoo.exceptions import ValidationError

from ..helpers.customer import pop_session_value


class PaymentMethodApplepay(models.Model):
    _inherit = "payment.method"

    buckaroo_official_applepay_merchant_guid = fields.Char(
        string="Buckaroo Merchant GUID",
        help="The Merchant GUID issued by Buckaroo for Apple Pay. "
        "Find this in Buckaroo Plaza under your Apple Pay configuration.",
        copy=False,
    )
    buckaroo_official_applepay_show_on_cart = fields.Boolean(
        string="Show on Cart",
        default=True,
        help="Display the Apple Pay express button on the cart page.",
        copy=False,
    )
    buckaroo_official_applepay_show_on_product = fields.Boolean(
        string="Show on Product",
        default=False,
        help="Display the Apple Pay express button on product detail pages. "
        "Tapping this button adds the chosen variant to the cart and "
        "starts the Apple Pay sheet.",
        copy=False,
    )
    buckaroo_official_applepay_show_on_checkout = fields.Boolean(
        string="Show on Checkout",
        default=True,
        help="Display Apple Pay as a regular payment method on the checkout "
        "page. When disabled, Apple Pay is hidden from the inline payment "
        "list (the cart-page express button is unaffected).",
        copy=False,
    )
    buckaroo_official_applepay_button_style = fields.Selection(
        [("black", "Dark"), ("white", "Light")],
        string="Button Style",
        default="black",
        help="Visual style of the Apple Pay button. Dark works on most "
        "storefronts; pick Light for dark-themed pages.",
        required=True,
        copy=False,
    )
    buckaroo_official_applepay_integration_mode = fields.Selection(
        [("inline", "Inline button"), ("redirect", "Redirect")],
        string="Integration Mode",
        default="inline",
        required=True,
        help="Inline renders the native Apple Pay button at checkout. Redirect "
        "uses Buckaroo's hosted Apple Pay page, which skips merchant domain "
        "verification but still requires a supported Apple device and browser.",
        copy=False,
    )

    def _get_compatible_payment_methods(
        self,
        provider_ids,
        partner_id,
        currency_id=None,
        force_tokenization=False,
        is_express_checkout=False,
        report=None,
        **kwargs,
    ):
        payment_methods = super()._get_compatible_payment_methods(
            provider_ids,
            partner_id,
            currency_id=currency_id,
            force_tokenization=force_tokenization,
            is_express_checkout=is_express_checkout,
            report=report,
            **kwargs,
        )
        return payment_methods.filtered(
            lambda pm: (
                pm.code != "buckaroo_applepay"
                or (
                    pm._buckaroo_applepay_is_configured()
                    and (
                        pm.buckaroo_official_applepay_show_on_cart
                        if is_express_checkout
                        else pm.buckaroo_official_applepay_show_on_checkout
                    )
                )
            )
        )

    def _buckaroo_applepay_is_configured(self):
        """Apple Pay needs only the Buckaroo Merchant GUID to be set."""
        self.ensure_one()
        return bool(self.buckaroo_official_applepay_merchant_guid)

    def _buckaroo_create_payment(self, transaction, client):
        # Redirect mode reuses the generic Buckaroo hosted flow (no Apple Pay
        # token, no domain verification); only the inline button path needs the
        # authorized token captured by the SDK.
        if (
            self.code != "buckaroo_applepay"
            or self.buckaroo_official_applepay_integration_mode == "redirect"
        ):
            return super()._buckaroo_create_payment(transaction, client)

        token = pop_session_value("buckaroo_applepay_token")
        customer_name = pop_session_value("buckaroo_applepay_customer_name")
        if not token or not customer_name:
            raise ValidationError(
                _("Apple Pay payment token is missing. Please retry the Apple Pay flow.")
            )

        # Buckaroo's API rejects the raw Apple Pay token; it must be base64.
        encoded_token = base64.b64encode(token.encode("utf-8")).decode("ascii")

        params = self._buckaroo_get_payment_params(transaction)
        builder = PaymentService(client).create_payment(
            self.buckaroo_official_sdk_service_name,
            params,
        )
        builder.add_parameter("PaymentData", encoded_token)
        builder.add_parameter("CustomerCardName", customer_name)
        return builder.pay()
