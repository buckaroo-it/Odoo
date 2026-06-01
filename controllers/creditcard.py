# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

from buckaroo.exceptions._buckaroo_error import BuckarooError
from buckaroo.services.hosted_fields_service import HostedFieldsService

from odoo import _, http
from odoo.http import request, route

from odoo.addons.website_sale.controllers.payment import PaymentPortal

_logger = logging.getLogger(__name__)


class CreditCardPaymentPortal(PaymentPortal):
    @route(
        "/shop/payment/transaction/<int:order_id>",
        type="jsonrpc",
        auth="public",
        website=True,
    )
    def shop_payment_transaction(self, order_id, access_token, **kwargs):
        hf_session_id = kwargs.pop("buckaroo_hf_session_id", None)
        hf_service = kwargs.pop("buckaroo_hf_service", None)
        cc_brand = kwargs.pop("buckaroo_cc_brand", None)
        if hf_session_id:
            request.session["buckaroo_hf_session_id"] = hf_session_id
            request.session["buckaroo_hf_service"] = hf_service or ""
        if cc_brand and isinstance(cc_brand, str) and len(cc_brand) <= 64:
            # Format guard only; the authoritative whitelist check against the
            # configured brands happens in payment_method_creditcard.
            request.session["buckaroo_cc_brand"] = cc_brand
        return super().shop_payment_transaction(order_id, access_token, **kwargs)


class CreditCardController(http.Controller):
    @http.route(
        "/payment/buckaroo_official/hosted-fields-token",
        type="jsonrpc",
        auth="public",
        methods=["POST"],
    )
    def hosted_fields_token(self, provider_id, payment_method_id=None, **_kwargs):
        """Fetch an OAuth token for Buckaroo Hosted Fields."""
        provider_sudo = request.env["payment.provider"].sudo().browse(int(provider_id))
        if (
            not provider_sudo.exists()
            or provider_sudo.code != "buckaroo_official"
            or provider_sudo.company_id != request.env.company
        ):
            return {"error": _("Invalid provider.")}

        if payment_method_id:
            pm_sudo = request.env["payment.method"].sudo().browse(int(payment_method_id))
        else:
            pm_sudo = provider_sudo.payment_method_ids.filtered(
                lambda m: m.code == "buckaroo_creditcard"
            )[:1]
        if not pm_sudo.exists():
            return {"error": _("Credit card payment method not found.")}
        if pm_sudo not in provider_sudo.payment_method_ids:
            return {"error": _("Invalid payment method for this provider.")}
        if pm_sudo.code != "buckaroo_creditcard":
            return {"error": _("Payment method is not a credit card.")}

        client_id = pm_sudo.buckaroo_official_hosted_fields_client_id
        client_secret = pm_sudo.buckaroo_official_hosted_fields_client_secret
        if not client_id or not client_secret:
            return {"error": _("Hosted Fields credentials are not configured.")}

        try:
            data = HostedFieldsService(client_id, client_secret).get_token()
            return {
                "access_token": data.get("access_token"),
                "expires_in": data.get("expires_in"),
            }
        except BuckarooError:
            _logger.exception("Failed to fetch Hosted Fields token from Buckaroo")
            return {"error": _("Failed to obtain Hosted Fields token.")}
