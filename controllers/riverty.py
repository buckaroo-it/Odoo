# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _
from odoo.exceptions import AccessError, ValidationError
from odoo.http import request, route

from odoo.addons.website_sale.controllers.payment import PaymentPortal

from ..helpers.customer import is_b2b, validate_bnpl_birthdate, validate_bnpl_registry


class RivertyPaymentPortal(PaymentPortal):
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
        # Validate the access token before reading any order metadata, so
        # this override can't become an is_b2b / NL-BE oracle for an
        # unauthenticated caller. Mirrors core's own check; NL/BE shoppers
        # must include Salutation or Riverty rejects the request later.
        try:
            order_sudo = self._document_check_access("sale.order", order_id, access_token)
        except AccessError as exc:
            raise ValidationError(_("The access token is invalid.")) from exc
        country_code = (
            order_sudo.partner_invoice_id.country_id.code
            if order_sudo.partner_invoice_id.country_id
            else ""
        )
        if country_code in ("NL", "BE") and not salutation:
            raise ValidationError(_("Please select a salutation to proceed with Riverty."))
        if salutation:
            request.session["buckaroo_riverty_salutation"] = salutation
            user = request.env.user
            if not user._is_public():
                user.partner_id.sudo().buckaroo_riverty_salutation = salutation

        # B2B shoppers must include an identification number (Chamber of
        # Commerce / VAT id); resolve B2B from the order's billing
        # partner, same as the country check above, so a logged-in
        # user can't bypass it via session.
        if is_b2b(order_sudo.partner_invoice_id):
            validate_bnpl_registry(
                kwargs,
                registry_kwarg="riverty_identification_number",
                session_key="buckaroo_riverty_identification_number",
                missing_msg=_("Please enter your identification number to proceed with Riverty."),
            )
        else:
            kwargs.pop("riverty_identification_number", None)
        return super().shop_payment_transaction(order_id, access_token, **kwargs)
