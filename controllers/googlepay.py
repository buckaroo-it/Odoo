# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo.http import request, route

from odoo.addons.website_sale.controllers.payment import PaymentPortal

from ..helpers.wallet import sanitize_customer_name, sanitize_token


class GooglepayPaymentPortal(PaymentPortal):
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
        if pm.code != "buckaroo_googlepay":
            return super().shop_payment_transaction(order_id, access_token, **kwargs)

        token = sanitize_token(kwargs.pop("buckaroo_googlepay_token", None))
        customer_name = sanitize_customer_name(kwargs.pop("buckaroo_googlepay_customer_name", None))
        if token:
            request.session["buckaroo_googlepay_token"] = token
        if customer_name:
            request.session["buckaroo_googlepay_customer_name"] = customer_name
        return super().shop_payment_transaction(order_id, access_token, **kwargs)
