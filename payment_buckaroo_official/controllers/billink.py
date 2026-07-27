# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _
from odoo.exceptions import AccessError, ValidationError
from odoo.http import request, route

from odoo.addons.website_sale.controllers.payment import PaymentPortal

from ..helpers.customer import is_b2b, validate_bnpl_birthdate, validate_bnpl_registry


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

        # Validate the access token before reading any order metadata, so
        # this override can't become an is_b2b oracle for an
        # unauthenticated caller. Mirrors core's own check.
        try:
            order_sudo = self._document_check_access("sale.order", order_id, access_token)
        except AccessError as exc:
            raise ValidationError(_("The access token is invalid.")) from exc
        if is_b2b(order_sudo.partner_invoice_id):
            validate_bnpl_registry(
                kwargs,
                registry_kwarg="billink_chamber_of_commerce",
                session_key="buckaroo_billink_chamber_of_commerce",
                missing_msg=_(
                    "Please enter your Chamber of Commerce number to proceed with Billink."
                ),
            )
        else:
            kwargs.pop("billink_chamber_of_commerce", None)

        return super().shop_payment_transaction(order_id, access_token, **kwargs)
