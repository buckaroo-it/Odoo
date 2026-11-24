# Part of Odoo. See LICENSE file for full copyright and licensing details.

import re

from werkzeug.exceptions import BadRequest

from odoo import _
from odoo.exceptions import UserError
from odoo.http import request, route

from odoo.addons.website_sale.controllers.payment import PaymentPortal

from ..utils import const

_CARDNUMBER_RE = re.compile(r"^[A-Za-z0-9-]{1,30}$")
_PIN_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")


class GiftcardPaymentPortal(PaymentPortal):
    @route(
        "/shop/payment/transaction/<int:order_id>",
        type="jsonrpc",
        auth="public",
        website=True,
    )
    def shop_payment_transaction(self, order_id, access_token, **kwargs):
        kwargs.pop("buckaroo_giftcard_brand", None)
        cardnumber = kwargs.pop("buckaroo_giftcard_cardnumber", None)
        pin = kwargs.pop("buckaroo_giftcard_pin", None)
        if cardnumber and pin and self._buckaroo_giftcard_request(kwargs):
            if not _CARDNUMBER_RE.match(cardnumber) or not _PIN_RE.match(pin):
                raise BadRequest(_("Invalid giftcard credentials."))
            request.update_context(
                buckaroo_giftcard_cardnumber=cardnumber,
                buckaroo_giftcard_pin=pin,
            )
        return super().shop_payment_transaction(order_id, access_token, **kwargs)

    def _validate_transaction_for_order(self, transaction, sale_order):
        result = super()._validate_transaction_for_order(transaction, sale_order)
        # A cheaper method picked after a partial giftcard payment can drop the
        # total to or below what the giftcard paid. Core only refuses an exact
        # match, so the new transaction would charge the whole total again
        # (Odoo 20's core refuses this case itself). Raising rolls back the
        # transaction just created. BTI-2182.
        currency = sale_order.currency_id
        if (
            transaction.state == "draft"
            and sale_order._buckaroo_giftcard_done_transactions()
            and currency.compare_amounts(sale_order.amount_paid, sale_order.amount_total) >= 0
        ):
            raise UserError(
                sale_order.env._("The giftcard already paid this order. Please refresh the page.")
            )
        if transaction.provider_code != const.PROVIDER_CODE:
            return result
        if transaction.state != "draft":
            return result
        if not sale_order._buckaroo_has_giftcard_pending_remainder():
            return result
        # Shrink the on-site remainder leg to the still-open amount. The leg is
        # left as an independent payment (no source_transaction_id link to the
        # giftcard slice): both legs reconcile and refund on their own PBNK; the
        # giftcard grouping lives on Buckaroo's side via the group transaction
        # key, not as an Odoo payment parent/child.
        remainder = sale_order._buckaroo_partial_payment_remainder()
        if sale_order.currency_id.compare_amounts(transaction.amount, remainder) != 0:
            transaction.amount = remainder
        return result

    @staticmethod
    def _buckaroo_giftcard_request(kwargs):
        method_id = kwargs.get("payment_method_id")
        if not method_id:
            return False
        method = request.env["payment.method"].sudo().browse(int(method_id)).exists()
        return bool(method) and method._is_linked_to_buckaroo() and method._is_buckaroo_giftcard()
