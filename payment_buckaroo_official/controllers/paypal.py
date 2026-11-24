# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo.http import request, route

from odoo.addons.website_sale.controllers.main import WebsiteSale
from odoo.addons.website_sale.controllers.payment import PaymentPortal

from ..helpers.wallet import sanitize_token


class PaypalWebsiteSale(WebsiteSale):
    def _get_shop_payment_values(self, order, **kwargs):
        """Add the amount the checkout PayPal button must authorize.

        The page renders ``amount`` with whatever surcharge the order holds,
        but paying with PayPal syncs PayPal's own surcharge first. The button
        creates the PayPal order from this amount, which must equal the
        transaction amount or Buckaroo refuses the pay request (BTI-2182).
        """
        values = super()._get_shop_payment_values(order, **kwargs)
        methods = values.get("payment_methods_sudo") or request.env["payment.method"]
        paypal = methods.filtered(lambda pm: pm.code == "buckaroo_paypal")[:1]
        if paypal:
            values["buckaroo_paypal_amount"] = order.sudo()._buckaroo_amount_total_for_method(
                paypal
            )
        return values


class PaypalPaymentPortal(PaymentPortal):
    @route(
        "/shop/payment/transaction/<int:order_id>",
        type="jsonrpc",
        auth="public",
        website=True,
    )
    def shop_payment_transaction(self, order_id, access_token, **kwargs):
        payment_method_id = kwargs.get("payment_method_id")
        if not payment_method_id:
            return super().shop_payment_transaction(order_id, access_token, **kwargs)
        pm = request.env["payment.method"].sudo().browse(int(payment_method_id))
        if pm.code != "buckaroo_paypal":
            return super().shop_payment_transaction(order_id, access_token, **kwargs)

        # The PayPal Express button captures the PayPal order id client-side and
        # forwards it here; stash it so `_buckaroo_create_payment` can pop it and
        # add `payPalOrderId` to the Buckaroo Pay request.
        paypal_order_id = sanitize_token(kwargs.pop("buckaroo_paypal_order_id", None))
        if paypal_order_id:
            request.session["buckaroo_paypal_order_id"] = paypal_order_id
        # Only the cart/product express button sends this, as a JSON `true`;
        # the transaction then takes the buyer's address from PayPal.
        if kwargs.pop("buckaroo_paypal_express", None) is True:
            request.update_context(buckaroo_paypal_express=True)
        try:
            result = super().shop_payment_transaction(order_id, access_token, **kwargs)
        except Exception:
            # Abort/error before `_buckaroo_create_payment` popped it: drop the
            # stashed id so a stale one can't linger across a later PayPal tx.
            request.session.pop("buckaroo_paypal_order_id", None)
            raise
        if isinstance(result, dict) and result.get("state") == "error":
            # Refused without raising (a checkout step failed): same reason.
            request.session.pop("buckaroo_paypal_order_id", None)
        return result
