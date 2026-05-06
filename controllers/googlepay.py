# Part of Odoo. See LICENSE file for full copyright and licensing details.

import re

from odoo.http import request

from odoo.addons.website_sale.controllers.payment import PaymentPortal


class GooglepayPaymentPortal(PaymentPortal):

    def shop_payment_transaction(self, order_id, access_token, **kwargs):
        payment_method_id = kwargs.get('payment_method_id')
        if not payment_method_id:
            return super().shop_payment_transaction(order_id, access_token, **kwargs)
        pm = request.env['payment.method'].sudo().browse(int(payment_method_id))
        if pm.code != 'googlepay':
            return super().shop_payment_transaction(order_id, access_token, **kwargs)

        token = self._buckaroo_sanitize_token(kwargs.pop('buckaroo_googlepay_token', None))
        customer_name = self._buckaroo_sanitize_customer_name(
            kwargs.pop('buckaroo_googlepay_customer_name', None)
        )
        if token:
            request.session['buckaroo_googlepay_token'] = token
        if customer_name:
            request.session['buckaroo_googlepay_customer_name'] = customer_name
        return super().shop_payment_transaction(order_id, access_token, **kwargs)

    @staticmethod
    def _buckaroo_sanitize_token(value):
        if value is None:
            return None
        cleaned = str(value).strip()
        if not cleaned:
            return None
        return cleaned[:8192]

    @staticmethod
    def _buckaroo_sanitize_customer_name(value):
        if value is None:
            return None
        cleaned = re.sub(r'[\x00-\x1F\x7F]', '', str(value)).strip()
        if not cleaned:
            return None
        return cleaned[:128]
