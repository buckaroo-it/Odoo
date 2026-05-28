# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _
from odoo.exceptions import ValidationError
from odoo.http import request, route

from odoo.addons.website_sale.controllers.payment import PaymentPortal


class KlarnaPaymentPortal(PaymentPortal):
    @route(
        "/shop/payment/transaction/<int:order_id>",
        type="jsonrpc",
        auth="public",
        website=True,
    )
    def shop_payment_transaction(self, order_id, access_token, **kwargs):
        payment_method_id = kwargs.get("payment_method_id")
        if payment_method_id:
            pm = request.env["payment.method"].sudo().browse(int(payment_method_id))
            if pm.code == "buckaroo_klarna":
                gender = kwargs.pop("klarna_gender", None)
                try:
                    gender_int = int(gender) if gender is not None else None
                except (TypeError, ValueError):
                    gender_int = None
                if gender_int not in (1, 2):
                    raise ValidationError(_("Please select your gender to proceed with Klarna."))
                gender_str = str(gender_int)
                request.session["buckaroo_klarna_gender"] = gender_str
                # Persist on the user's partner so the next checkout can prefill.
                user = request.env.user
                if not user._is_public():
                    user.partner_id.sudo().buckaroo_klarna_gender = gender_str
        kwargs.pop("klarna_gender", None)
        return super().shop_payment_transaction(order_id, access_token, **kwargs)
