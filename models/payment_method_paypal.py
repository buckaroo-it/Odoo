# Part of Odoo. See LICENSE file for full copyright and licensing details.

from buckaroo.services.payment_service import PaymentService

from odoo import _, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import email_normalize

from ..helpers.customer import (
    get_billing_partner,
    get_shipping_partner,
    pop_session_value,
)
from ..utils import const


class PaymentMethodPaypal(models.Model):
    _inherit = "payment.method"

    buckaroo_official_paypal_merchant_id = fields.Char(
        string="PayPal Merchant ID",
        help="The live PayPal merchant identifier linked to your Buckaroo PayPal "
        "configuration. Used when the provider runs in production mode.",
        copy=False,
    )
    buckaroo_official_paypal_sandbox_merchant_id = fields.Char(
        string="PayPal Sandbox Merchant ID",
        help="The PayPal sandbox merchant identifier. Used when the provider "
        "runs in test mode, where Buckaroo creates a sandbox PayPal order that "
        "must be approved under the matching sandbox merchant.",
        copy=False,
    )
    buckaroo_official_paypal_show_on_cart = fields.Boolean(
        string="Show on Cart",
        default=True,
        help="Display the PayPal Express button on the cart page.",
        copy=False,
    )
    buckaroo_official_paypal_show_on_product = fields.Boolean(
        string="Show on Product",
        default=False,
        help="Display the PayPal Express button on product detail pages. "
        "Tapping this button adds the chosen variant to the cart and "
        "starts the PayPal Express flow.",
        copy=False,
    )
    buckaroo_official_paypal_show_on_checkout = fields.Boolean(
        string="Show on Checkout",
        default=True,
        help="Display PayPal as a regular payment method on the checkout "
        "page. When disabled, PayPal is hidden from the inline payment "
        "list (the cart-page express button is unaffected).",
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
                pm.code != "buckaroo_paypal"
                or (
                    pm._buckaroo_paypal_is_configured()
                    and (
                        pm.buckaroo_official_paypal_show_on_cart
                        if is_express_checkout
                        else pm.buckaroo_official_paypal_show_on_checkout
                    )
                )
            )
        )

    def _buckaroo_paypal_is_configured(self):
        """Configured when the merchant id for the active provider mode is set:
        the sandbox id in test mode, the live id in production."""
        self.ensure_one()
        provider = self._buckaroo_paypal_active_provider()
        return bool(provider) and bool(self._buckaroo_paypal_merchant_id_for(provider))

    def _buckaroo_paypal_active_provider(self):
        """The linked, enabled-or-test Buckaroo provider (one mode at a time)."""
        self.ensure_one()
        return self.provider_ids.filtered(
            lambda p: p.code == const.PROVIDER_CODE and p.state in ("enabled", "test")
        )[:1]

    def _buckaroo_paypal_merchant_id_for(self, provider):
        """Sandbox merchant id in test mode, live merchant id otherwise. The SDK
        environment (test vs live) is selected client-side from the same provider
        state, so the merchant must match it or the order can't be captured."""
        self.ensure_one()
        if provider and provider.state == "test":
            return self.buckaroo_official_paypal_sandbox_merchant_id or ""
        return self.buckaroo_official_paypal_merchant_id or ""

    def _buckaroo_create_payment(self, transaction, client):
        if self.code != "buckaroo_paypal":
            return super()._buckaroo_create_payment(transaction, client)

        order_id = pop_session_value("buckaroo_paypal_order_id")
        if not order_id:
            raise ValidationError(
                _("PayPal order ID is missing. Please retry the PayPal Express flow.")
            )

        params = self._buckaroo_get_payment_params(transaction)
        builder = PaymentService(client).create_payment(
            self.buckaroo_official_sdk_service_name,
            params,
        )
        builder.add_parameter("payPalOrderId", order_id)
        transaction.buckaroo_official_is_paypal_express = True
        return builder.pay()

    def _buckaroo_handle_no_redirect_response(self, transaction, response):
        """PayPal Express captures during ``pay``: a successful response carries
        no redirect URL (the buyer already approved in the PayPal sheet), so the
        generic flow would mistake it for an error. Settle the tx from the sync
        response - which also carries the buyer's address - and route via
        ``/shop/payment/validate`` so the order is confirmed (mirrors Bank
        Transfer's inline-completion handling)."""
        if self.code != "buckaroo_paypal":
            return super()._buckaroo_handle_no_redirect_response(transaction, response)
        status_code = (
            response.status.code.code if response.status and response.status.code else None
        )
        if status_code == const.BuckarooStatusCode.SUCCESS:
            transaction.provider_reference = response.key
            self._buckaroo_paypal_write_address(transaction, response)
            transaction._set_done()
        elif response.is_pending():
            transaction.provider_reference = response.key
            transaction._set_pending()
        else:
            return None
        base_url = transaction.provider_id.get_base_url().rstrip("/")
        return {"api_url": f"{base_url}/shop/payment/validate"}

    def _buckaroo_apply_push_metadata(self, transaction, payment_data):
        if self.code != "buckaroo_paypal":
            return super()._buckaroo_apply_push_metadata(transaction, payment_data)
        super()._buckaroo_apply_push_metadata(transaction, payment_data)
        self._buckaroo_paypal_write_address(transaction, payment_data)

    def _buckaroo_paypal_write_address(self, transaction, source):
        """Overwrite the checkout-time stub partner ("PayPal Customer", empty
        address) with the buyer's real PayPal address. *source* is a push or the
        sync pay response; both expose ``get_service_parameter``. Only express
        transactions carry these params."""
        if not transaction.buckaroo_official_is_paypal_express:
            return
        vals = self._buckaroo_paypal_address_vals(source)
        if not vals:
            return
        billing = get_billing_partner(transaction)
        shipping = get_shipping_partner(transaction)
        partners = billing
        if shipping and shipping != billing:
            partners |= shipping
        partners.write(vals)

    def _buckaroo_paypal_address_vals(self, payment_data):
        """Map PayPal Express push params to ``res.partner`` write values.

        ``address_line_1`` is the full street line including the house number
        (PayPal does not split it), so it maps straight to ``street``.
        ``admin_area_2`` is the city. Email is dropped unless it validates.
        Unresolvable country codes are left untouched.
        """
        get = payment_data.get_service_parameter
        first = (get("payerFirstname") or "").strip()
        last = (get("payerLastname") or "").strip()
        street = (get("address_line_1") or "").strip()
        city = (get("admin_area_2") or "").strip()
        zip_code = (get("postal_code") or "").strip()
        country_code = (get("payerCountry") or "").strip()
        email = (get("payerEmail") or "").strip()

        vals = {}
        name = f"{first} {last}".strip()
        if name:
            vals["name"] = name
        if street:
            vals["street"] = street
        if city:
            vals["city"] = city
        if zip_code:
            vals["zip"] = zip_code
        if country_code:
            country = self.env["res.country"].search(
                [("code", "=", country_code.upper())], limit=1
            )
            if country:
                vals["country_id"] = country.id
        normalized_email = email_normalize(email) if email else False
        if normalized_email:
            vals["email"] = normalized_email
        return vals


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    buckaroo_official_is_paypal_express = fields.Boolean(
        string="PayPal Express Checkout",
        help="Set when this transaction was created through the PayPal Express "
        "flow, so the push handler overwrites the checkout-time stub partner "
        "with the PayPal-supplied address.",
        copy=False,
    )
