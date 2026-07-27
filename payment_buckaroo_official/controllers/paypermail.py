# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo.http import request, route

from odoo.addons.website_sale.controllers.payment import PaymentPortal


class PayPerEmailPaymentPortal(PaymentPortal):
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
        if pm.code != "buckaroo_paypermail":
            return super().shop_payment_transaction(order_id, access_token, **kwargs)

        # Gender is optional for PPE: the select sends "1"/"2" or nothing, so
        # store only a valid pick and pass through silently otherwise.
        gender = kwargs.pop("paypermail_gender", None)
        if gender in ("1", "2"):
            request.session["buckaroo_paypermail_gender"] = gender
            # Persist on the user's partner so the next checkout can prefill.
            user = request.env.user
            if not user._is_public():
                user.partner_id.sudo().buckaroo_paypermail_gender = gender
        return super().shop_payment_transaction(order_id, access_token, **kwargs)
