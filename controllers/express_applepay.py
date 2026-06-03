# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import http

from odoo.addons.website_sale.controllers.cart import Cart

from .express_wallet import BuckarooWalletExpressMixin


class BuckarooApplepayExpressController(BuckarooWalletExpressMixin, Cart):
    @http.route(
        "/shop/buckaroo/applepay/express_init",
        type="jsonrpc",
        auth="public",
        methods=["POST"],
        website=True,
        sitemap=False,
    )
    def applepay_express_init(
        self,
        product_id=None,
        qty=None,
        no_variant_attribute_value_ids=None,
        product_custom_attribute_values=None,
    ):
        return self._buckaroo_wallet_express_init(
            product_id=product_id,
            qty=qty,
            no_variant_attribute_value_ids=no_variant_attribute_value_ids,
            product_custom_attribute_values=product_custom_attribute_values,
        )
