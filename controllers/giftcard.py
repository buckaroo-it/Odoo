# Part of Odoo. See LICENSE file for full copyright and licensing details.

import re

from werkzeug.exceptions import BadRequest

from odoo import _
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
        if transaction.provider_code != const.PROVIDER_CODE:
            return result
        if transaction.state != "draft":
            return result
        if not sale_order._buckaroo_has_giftcard_pending_remainder():
            return result
        remainder = sale_order._buckaroo_partial_payment_remainder()
        if sale_order.currency_id.compare_amounts(transaction.amount, remainder) != 0:
            transaction.amount = remainder
        # Chain this inline leg into the giftcard payment group so refund and
        # traceability code that walks source_transaction_id sees the same
        # graph the redirect GCR flow builds. First-wins: never re-point a leg
        # that already has a source (e.g. a redirect-spawned GCR child). The
        # root is always a done tx and this leg is draft, so the two can never
        # be the same record.
        if not transaction.source_transaction_id:
            root = sale_order._buckaroo_giftcard_root_transaction()
            if root:
                transaction.source_transaction_id = root.id
        return result

    @staticmethod
    def _buckaroo_giftcard_request(kwargs):
        method_id = kwargs.get("payment_method_id")
        if not method_id:
            return False
        method = request.env["payment.method"].sudo().browse(int(method_id)).exists()
        return bool(method) and method._is_linked_to_buckaroo() and method._is_buckaroo_giftcard()
