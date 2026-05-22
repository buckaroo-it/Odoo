# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _
from odoo.exceptions import ValidationError
from odoo.http import request

from odoo.addons.website_sale.controllers.payment import PaymentPortal

from ..helpers.customer import validate_bnpl_birthdate


class RivertyPaymentPortal(PaymentPortal):
    def shop_payment_transaction(self, order_id, access_token, **kwargs):
        payment_method_id = kwargs.get("payment_method_id")
        if not payment_method_id:
            return super().shop_payment_transaction(order_id, access_token, **kwargs)
        pm = request.env["payment.method"].sudo().browse(int(payment_method_id))
        if pm.code != "buckaroo_riverty":
            return super().shop_payment_transaction(order_id, access_token, **kwargs)

        salutation = kwargs.pop("riverty_salutation", None) or ""
        if salutation and salutation not in ("Mr", "Mrs", "Miss"):
            raise ValidationError(_("Invalid salutation; choose Mr, Mrs, or Miss."))
        validate_bnpl_birthdate(
            kwargs,
            birthdate_kwarg="riverty_birthdate",
            session_key="buckaroo_riverty_birthdate",
            partner_field="buckaroo_riverty_birthdate",
            missing_msg=_("Please enter your date of birth to proceed with Riverty."),
            invalid_msg=_("Invalid date of birth."),
            underage_msg=_("You must be at least 18 years old to use Riverty."),
        )
        # NL/BE shoppers must include Salutation; Riverty rejects the
        # request later otherwise. Resolve country from the order's
        # billing partner so a logged-in user can't bypass via session.
        order = request.env["sale.order"].sudo().browse(int(order_id)).exists()
        country_code = (
            order.partner_invoice_id.country_id.code
            if order and order.partner_invoice_id.country_id
            else ""
        )
        if country_code in ("NL", "BE") and not salutation:
            raise ValidationError(_("Please select a salutation to proceed with Riverty."))
        if salutation:
            request.session["buckaroo_riverty_salutation"] = salutation
            user = request.env.user
            if not user._is_public():
                user.partner_id.sudo().buckaroo_riverty_salutation = salutation
        return super().shop_payment_transaction(order_id, access_token, **kwargs)
