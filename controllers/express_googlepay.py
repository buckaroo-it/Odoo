# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _, http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.website_sale.controllers.cart import Cart


class BuckarooGooglepayExpressController(Cart):

    def _get_express_shop_payment_values(self, order, **kwargs):
        # Wallet popup must show final price; pick cheapest carrier
        # now so amount = amount_total at popup time.
        if order._has_deliverable_products() and not order.carrier_id:
            methods = order._get_delivery_methods()
            if methods:
                cheapest = min(
                    methods,
                    key=lambda m: m.rate_shipment(order).get('price', float('inf')),
                )
                order._set_delivery_method(cheapest)
        return super()._get_express_shop_payment_values(order, **kwargs)

    @http.route(
        '/shop/buckaroo/googlepay/express_init',
        type='jsonrpc', auth='public', methods=['POST'], website=True, sitemap=False,
    )
    def googlepay_express_init(
        self, product_id=None, qty=None,
        no_variant_attribute_value_ids=None, product_custom_attribute_values=None,
    ):
        if not product_id or qty is None:
            raise UserError(_("Missing product_id or qty."))
        try:
            pid = int(product_id)
            quantity = int(qty)
        except (TypeError, ValueError):
            raise UserError(_("Invalid product_id or qty."))
        if quantity <= 0:
            raise UserError(_("Quantity must be positive."))

        product = request.env['product.product'].browse(pid).exists()
        if not product:
            raise UserError(_("The given product does not exist."))

        add_kwargs = {}
        if no_variant_attribute_value_ids is not None:
            add_kwargs['no_variant_attribute_value_ids'] = no_variant_attribute_value_ids
        if product_custom_attribute_values is not None:
            add_kwargs['product_custom_attribute_values'] = product_custom_attribute_values
        self.add_to_cart(
            product_template_id=product.product_tmpl_id.id,
            product_id=pid,
            quantity=quantity,
            **add_kwargs,
        )

        order_sudo = request.cart
        raw = self._get_express_shop_payment_values(order_sudo)
        currency = raw.get('currency') or order_sudo.currency_id
        # Whitelist JSON-safe scalars; raw contains recordsets.
        return {
            'amount': raw.get('amount'),
            'minor_amount': raw.get('minor_amount'),
            'currency_code': currency.name,
            'partner_id': raw.get('partner_id'),
            'transaction_route': raw.get('transaction_route'),
            'express_checkout_route': raw.get('express_checkout_route'),
            'shipping_info_required': bool(raw.get('shipping_info_required')),
            'landing_route': raw.get('landing_route') or '/shop/payment/validate',
            'access_token': raw.get('payment_access_token'),
        }
