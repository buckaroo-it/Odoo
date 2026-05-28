# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _
from odoo.exceptions import ValidationError
from odoo.http import request, route

from odoo.addons.website_sale.controllers.payment import PaymentPortal

from ..helpers.customer import validate_bnpl_birthdate


class BillinkPaymentPortal(PaymentPortal):
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
        if pm.code != "buckaroo_billink":
            return super().shop_payment_transaction(order_id, access_token, **kwargs)

        if not kwargs.pop("billink_tc_accepted", False):
            raise ValidationError(_("Please accept the Billink Terms and Conditions to proceed."))
        validate_bnpl_birthdate(
            kwargs,
            birthdate_kwarg="billink_birthdate",
            session_key="buckaroo_billink_birthdate",
            partner_field="buckaroo_billink_birthdate",
            missing_msg=_("Please enter your date of birth to proceed with Billink."),
            invalid_msg=_("Invalid date of birth."),
            underage_msg=_("You must be at least 18 years old to use Billink."),
        )
        return super().shop_payment_transaction(order_id, access_token, **kwargs)
