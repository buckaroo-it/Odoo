# Part of Odoo. See LICENSE file for full copyright and licensing details.

from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request

from odoo.addons.website_sale.controllers.main import WebsiteSale

from .express_wallet import BuckarooWalletExpressMixin


class BuckarooWalletExpressCheckout(WebsiteSale):
    """Apply express-wallet billing/shipping to the dedicated buy-now order.

    Core's ``process_express_checkout`` records the address on ``request.cart``,
    so this points it at the express order (see ``_buckaroo_wallet_express_init``)
    for the call, leaving the session cart untouched.
    """

    @http.route(
        BuckarooWalletExpressMixin.EXPRESS_CHECKOUT_ROUTE,
        type="jsonrpc",
        auth="public",
        methods=["POST"],
        website=True,
        sitemap=False,
    )
    def buckaroo_wallet_express_checkout(
        self, billing_address, shipping_address=None, shipping_option=None, **kwargs
    ):
        express_sudo = self._buckaroo_express_order_sudo()
        if not express_sudo:
            # Out of sync: error rather than record the address on the real cart.
            raise NotFound()
        # Honor the authorized total: freeze fiscal position + pricelist while the
        # address applies, else a foreign-jurisdiction address recomputes taxes
        # and diverges the charge. (Core pins pricelist on its anonymous path.)
        pinned = [
            express_sudo._fields["fiscal_position_id"],
            express_sudo._fields["pricelist_id"],
        ]
        with BuckarooWalletExpressMixin._buckaroo_preserve_cart_badge():
            request.cart = express_sudo
            with request.env.protecting(pinned, express_sudo):
                partner_id = self.process_express_checkout(
                    billing_address=billing_address,
                    shipping_address=shipping_address,
                    shipping_option=shipping_option,
                    **kwargs,
                )
                if shipping_option and not shipping_address:
                    self._buckaroo_apply_shipping_option(express_sudo, shipping_option)
        # Single-use: drop the key so a stale id can't linger across later
        # (possibly different-identity) requests. The tx route uses id + token.
        request.session.pop(BuckarooWalletExpressMixin.EXPRESS_ORDER_SESSION_KEY, None)
        return partner_id

    @staticmethod
    def _buckaroo_apply_shipping_option(order_sudo, shipping_option):
        """Set the carrier a sheet without a shipping address priced (Google
        Pay on the product page). Core applies ``shipping_option`` only with a
        shipping address, so the order would keep another carrier and charge a
        different total than the sheet showed (BTI-2182)."""
        try:
            dm_id = int(shipping_option.get("id"))
        except (AttributeError, TypeError, ValueError):
            return
        carrier = order_sudo._get_delivery_methods().filtered(lambda dm: dm.id == dm_id)
        if carrier:
            order_sudo._set_delivery_method(carrier)

    def _buckaroo_express_order_sudo(self):
        """Pending express order from the session, or empty.

        From the session (not a param, so it can't be retargeted) and only an
        unpaid draft on this website owned by the current partner - a stale or
        foreign id is inert.
        """
        order_id = request.session.get(BuckarooWalletExpressMixin.EXPRESS_ORDER_SESSION_KEY)
        if not order_id:
            return request.env["sale.order"]
        order_sudo = request.env["sale.order"].sudo().browse(order_id).exists()
        if (
            order_sudo
            and order_sudo.state == "draft"
            and order_sudo.website_id == request.env.website
            and order_sudo.partner_id == request.env.user.partner_id
        ):
            return order_sudo
        return request.env["sale.order"]
