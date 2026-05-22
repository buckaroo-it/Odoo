# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _
from odoo.http import request

from odoo.addons.website_sale.controllers.payment import PaymentPortal

from ..helpers.customer import validate_bnpl_birthdate


class In3PaymentPortal(PaymentPortal):
    def shop_payment_transaction(self, order_id, access_token, **kwargs):
        payment_method_id = kwargs.get("payment_method_id")
        if not payment_method_id:
            return super().shop_payment_transaction(order_id, access_token, **kwargs)
        pm = request.env["payment.method"].sudo().browse(int(payment_method_id))
        if pm.code != "buckaroo_in3":
            return super().shop_payment_transaction(order_id, access_token, **kwargs)

        validate_bnpl_birthdate(
            kwargs,
            birthdate_kwarg="in3_birthdate",
            session_key="buckaroo_in3_birthdate",
            partner_field="buckaroo_in3_birthdate",
            missing_msg=_("Please enter your date of birth to proceed with In3."),
            invalid_msg=_("Invalid date of birth."),
            underage_msg=_("You must be at least 18 years old to use In3."),
        )
        return super().shop_payment_transaction(order_id, access_token, **kwargs)
