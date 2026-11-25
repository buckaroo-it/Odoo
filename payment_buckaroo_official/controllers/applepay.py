# Part of Odoo. See LICENSE file for full copyright and licensing details.

from functools import partial
from werkzeug.wrappers import Response

from odoo import _
from odoo.exceptions import ValidationError
from odoo.http import Controller, request, route
from odoo.tools import file_open

from odoo.addons.website_sale.controllers.payment import PaymentPortal

from ..helpers.wallet import (
    call_dropping_session_on_failure,
    is_amex_network,
    sanitize_customer_name,
    sanitize_token,
)
from ..utils import const


class ApplepayPaymentPortal(PaymentPortal):
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
        if pm.code != "buckaroo_applepay":
            return super().shop_payment_transaction(order_id, access_token, **kwargs)

        if is_amex_network(kwargs.pop("buckaroo_applepay_network", None)):
            raise ValidationError(
                _("American Express is not supported for Apple Pay. Please choose another card.")
            )
        token = sanitize_token(kwargs.pop("buckaroo_applepay_token", None))
        customer_name = sanitize_customer_name(kwargs.pop("buckaroo_applepay_customer_name", None))
        if token:
            request.session["buckaroo_applepay_token"] = token
            # The sheet total check in `fee.py` must run for every token.
            request.update_context(**{const.WALLET_SHEET_REQUIRED_CONTEXT_KEY: True})
        if customer_name:
            request.session["buckaroo_applepay_customer_name"] = customer_name
        return call_dropping_session_on_failure(
            request.session,
            ("buckaroo_applepay_token", "buckaroo_applepay_customer_name"),
            partial(super().shop_payment_transaction, order_id, access_token, **kwargs),
        )


class ApplepayWellKnownController(Controller):
    _DOMAIN_ASSOCIATION_FILE = (
        "payment_buckaroo_official/static/files/apple-developer-merchantid-domain-association"
    )

    # No ``website=True``: Apple fetches the exact root path server-side and
    # will not follow a locale redirect, so this must stay a plain http route.
    @route(
        "/.well-known/apple-developer-merchantid-domain-association",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        sitemap=False,
    )
    def apple_pay_get_domain_association_file(self, **kwargs):
        with file_open(self._DOMAIN_ASSOCIATION_FILE) as f:
            body = f.read()
        return Response(body, status=200, mimetype="text/plain")
