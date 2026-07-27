# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo.http import request, route

from odoo.addons.website_sale.controllers.payment import PaymentPortal

from ..helpers.wallet import sanitize_token


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
        try:
            return super().shop_payment_transaction(order_id, access_token, **kwargs)
        except Exception:
            # Abort/error before `_buckaroo_create_payment` popped it: drop the
            # stashed id so a stale one can't linger across a later PayPal tx.
            request.session.pop("buckaroo_paypal_order_id", None)
            raise
