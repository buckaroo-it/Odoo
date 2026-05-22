# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import http
from odoo.http import request
from odoo.tools import formatLang

from odoo.addons.website_sale.controllers.payment import PaymentPortal

from ..utils import const


class BuckarooFeePaymentPortal(PaymentPortal):
    def shop_payment_transaction(self, order_id, access_token, **kwargs):
        if not self._buckaroo_official_request_for_buckaroo(kwargs):
            return super().shop_payment_transaction(order_id, access_token, **kwargs)
        if order_id:
            method_id = self._buckaroo_resolve_pick_method_id(
                kwargs.get("token_id"),
                kwargs.get("payment_method_id"),
            )
            if method_id:
                request.update_context(**{const.PICK_METHOD_CONTEXT_KEY: method_id})
        kwargs.pop("amount", None)
        return super().shop_payment_transaction(order_id, access_token, **kwargs)

    @staticmethod
    def _buckaroo_official_request_for_buckaroo(kwargs):
        """``True`` when the inbound request targets the Buckaroo provider.
        Gates Buckaroo-only mutations (``amount`` pop, pick-method context)
        so other providers' kwargs are passed through to super untouched."""
        provider_id = kwargs.get("provider_id")
        if provider_id:
            provider = request.env["payment.provider"].sudo().browse(int(provider_id)).exists()
            return bool(provider) and provider.code == const.PROVIDER_CODE
        method_id = kwargs.get("payment_method_id")
        if method_id:
            method = request.env["payment.method"].sudo().browse(int(method_id)).exists()
            return bool(method) and method._is_linked_to_buckaroo()
        token_id = kwargs.get("token_id")
        if token_id:
            token = request.env["payment.token"].sudo().browse(int(token_id)).exists()
            return bool(token) and token.provider_id.code == const.PROVIDER_CODE
        return False

    @staticmethod
    def _buckaroo_resolve_pick_method_id(token_id, payment_method_id):
        # Token wins: form's payment_method_id may be stale from an earlier click.
        if token_id:
            token = request.env["payment.token"].sudo().browse(int(token_id)).exists()
            return token.payment_method_id.id if token else None
        if payment_method_id:
            return int(payment_method_id)
        return None

    @http.route(
        "/payment/buckaroo_official/set_method",
        type="jsonrpc",
        auth="public",
        website=True,
    )
    def buckaroo_official_set_method(
        self,
        payment_method_id=None,
        token_id=None,
        **_kwargs,
    ):
        if not (order := request.cart) or order.state not in ("draft", "sent"):
            return {}
        method_id = self._buckaroo_resolve_pick_method_id(token_id, payment_method_id)
        if method_id:
            pm = request.env["payment.method"].sudo().browse(method_id).exists()
            if pm:
                order._buckaroo_sync_surcharge_for_method(pm)
        env = request.env
        currency = order.currency_id
        return {
            "amount_untaxed": formatLang(env, order.amount_untaxed, currency_obj=currency),
            "amount_tax": formatLang(env, order.amount_tax, currency_obj=currency),
            "amount_total": formatLang(env, order.amount_total, currency_obj=currency),
            "amount_buckaroo_surcharge": formatLang(
                env,
                order.amount_buckaroo_surcharge,
                currency_obj=currency,
            ),
            "has_buckaroo_surcharge": bool(order.amount_buckaroo_surcharge),
        }
