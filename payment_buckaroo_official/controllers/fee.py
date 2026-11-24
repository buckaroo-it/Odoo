# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

from psycopg2 import OperationalError

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request, route
from odoo.tools import float_compare, formatLang

from odoo.addons.website_sale.controllers.main import WebsiteSale
from odoo.addons.website_sale.controllers.payment import PaymentPortal

from ..utils import const

_logger = logging.getLogger(__name__)


class BuckarooFeeWebsiteSale(WebsiteSale):
    def _get_shop_payment_values(self, order, **kwargs):
        """Add the amount each checkout wallet button shows or authorizes, by
        payment method id.

        The page renders ``amount`` with whatever surcharge the order holds,
        but paying syncs the picked method's own surcharge first, and after a
        partial giftcard payment only the open remainder is charged. PayPal
        authorizes this amount up front, so it must equal the transaction
        amount or Buckaroo refuses the pay request; the Apple Pay and Google Pay
        sheets show it to the buyer (BTI-2182). Keyed by id: several records
        with one code (one per provider on Odoo 20) can carry different fees.
        """
        values = super()._get_shop_payment_values(order, **kwargs)
        methods = values.get("payment_methods_sudo") or request.env["payment.method"]
        wallets = methods.filtered(lambda pm: pm.code in const.WALLET_METHOD_CODES)
        order_sudo = order.sudo()
        amounts = {}
        for pm in wallets:
            # A failing preview must not break the page for every method. That
            # wallet gets no amount (`None`): the page says it's unavailable
            # rather than offer an amount the transaction won't charge.
            try:
                amounts[pm.id] = order_sudo._buckaroo_amount_to_pay_for_method(pm)
            except OperationalError:
                raise  # A concurrency error: Odoo retries the request.
            except Exception:
                _logger.exception("Buckaroo wallet amount preview failed for %s", pm.code)
                amounts[pm.id] = None
        if amounts:
            values["buckaroo_wallet_amounts"] = amounts
        return values


class BuckarooFeePaymentPortal(PaymentPortal):
    @route(
        "/shop/payment/transaction/<int:order_id>",
        type="jsonrpc",
        auth="public",
        website=True,
    )
    def shop_payment_transaction(self, order_id, access_token, **kwargs):
        # Checked in `_validate_transaction_for_order`. Popped for every
        # request: core rejects keys it doesn't know.
        sheet_amount = kwargs.pop("buckaroo_wallet_amount", None)
        if sheet_amount is not None:
            request.update_context(**{const.WALLET_SHEET_AMOUNT_CONTEXT_KEY: sheet_amount})
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

    def _validate_transaction_for_order(self, transaction, sale_order):
        result = super()._validate_transaction_for_order(transaction, sale_order)
        self._buckaroo_check_wallet_sheet_amount(transaction, sale_order)
        return result

    @staticmethod
    def _buckaroo_check_wallet_sheet_amount(transaction, sale_order):
        """Refuse to charge another amount than the Apple Pay / Google Pay
        sheet showed when the buyer approved it.

        The sheet total is computed before the payment (a product page rates
        its carrier off a throwaway order, for one), so the order can still
        differ when the transaction is created, e.g. a carrier priced by the
        buyer's address. Raising rolls back the transaction before Buckaroo
        is called, so nothing is charged (BTI-2182). Compares with the amount
        to pay, not ``transaction.amount``: another hook may still shrink the
        transaction to an open giftcard remainder.
        """
        context = transaction.env.context
        sheet_amount = context.get(const.WALLET_SHEET_AMOUNT_CONTEXT_KEY)
        if transaction.state != "draft":
            return
        # A wallet token without its sheet total (e.g. a page loaded before an
        # upgrade) can't be checked: refuse it rather than charge unchecked.
        if sheet_amount is None and not context.get(const.WALLET_SHEET_REQUIRED_CONTEXT_KEY):
            return
        try:
            sheet_amount = float(sheet_amount)
        except (TypeError, ValueError):
            sheet_amount = None
        expected = sale_order.sudo()._buckaroo_amount_to_pay_for_method(
            transaction.payment_method_id
        )
        # The sheets show 2 decimals; a 3-decimal currency can't match closer.
        digits = min(sale_order.currency_id.decimal_places, 2)
        if sheet_amount is None or float_compare(sheet_amount, expected, precision_digits=digits):
            raise ValidationError(
                sale_order.env._(
                    "The total changed while you were paying, so nothing was charged. "
                    "Please check the amount and try again."
                )
            )

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
