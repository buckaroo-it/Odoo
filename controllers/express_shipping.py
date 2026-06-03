# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import http
from odoo.http import request

from odoo.addons.payment import utils as payment_utils
from odoo.addons.website_sale.controllers.delivery import Delivery


class BuckarooWalletShippingController(Delivery):
    """In-sheet shipping selection for the Buckaroo Apple Pay / Google Pay
    express buttons.

    Each wallet fires a callback while its sheet is open: once when the buyer
    confirms an address (the sheet needs the available delivery methods) and
    once when they switch method (the sheet needs the new total). Both routes
    reuse Odoo's express delivery machinery and return the server-authoritative
    order total, so the wallet charges exactly what the sale order totals.
    """

    _EMPTY_AMOUNTS = {"amount": 0.0, "minor_amount": 0, "currency_code": ""}

    class _DiscardOrder(Exception):
        """Sentinel raised to roll back the throwaway product-rating order."""

    @http.route(
        "/shop/buckaroo/wallet/shipping_address",
        type="jsonrpc",
        auth="public",
        methods=["POST"],
        website=True,
        sitemap=False,
    )
    def buckaroo_wallet_shipping_address(self, partial_delivery_address):
        if not request.cart:
            return {"delivery_methods": [], **self._EMPTY_AMOUNTS}
        # Odoo applies the (redacted) address, preselects the cheapest carrier
        # and returns the methods sorted cheapest-first.
        res = self.express_checkout_process_delivery_address(partial_delivery_address)
        methods = res.get("delivery_methods", []) if isinstance(res, dict) else []
        currency = request.cart.currency_id
        return {
            "delivery_methods": [
                {
                    "id": m["id"],
                    "name": m["name"],
                    "amount": payment_utils.to_major_currency_units(m["minorAmount"], currency),
                }
                for m in methods
            ],
            **self._buckaroo_wallet_amounts(),
        }

    @http.route(
        "/shop/buckaroo/wallet/methods",
        type="jsonrpc",
        auth="public",
        methods=["POST"],
        website=True,
        sitemap=False,
    )
    def buckaroo_wallet_methods(self):
        """Available carriers for the cart's current address, used to seed the
        inline Apple Pay sheet so the method selector shows on open (the order
        already has the address from checkout, so no address callback fires)."""
        order_sudo = request.cart
        if not order_sudo:
            return {"delivery_methods": [], "selected_id": None, **self._EMPTY_AMOUNTS}
        currency = order_sudo.currency_id
        methods = sorted(
            self._get_delivery_methods_express_checkout(order_sudo).items(),
            key=lambda item: item[1],
        )
        return {
            "delivery_methods": [
                {"id": carrier.id, "name": carrier.name, "amount": currency.round(price)}
                for carrier, price in methods
            ],
            "selected_id": order_sudo.carrier_id.id or None,
            **self._buckaroo_wallet_amounts(),
        }

    @http.route(
        "/shop/buckaroo/wallet/product_methods",
        type="jsonrpc",
        auth="public",
        methods=["POST"],
        website=True,
        sitemap=False,
    )
    def buckaroo_wallet_product_methods(self, product_id, qty=1):
        """Carriers for a product page, where no cart exists yet. Rates them off
        a throwaway order (rolled back), so the express sheet can seed its
        method selector before anything is added to the cart. Total math is then
        done client-side until the real cart is created on authorize."""
        empty = {"delivery_methods": [], "subtotal": 0.0, "currency_code": ""}
        try:
            pid, quantity = int(product_id), max(int(qty), 1)
        except (TypeError, ValueError):
            return empty
        product = request.env["product.product"].sudo().browse(pid).exists()
        if not product:
            return empty
        website = request.website
        # Same fpos/pricelist source as the real express order, so the sheet
        # subtotal equals what we later charge.
        partner = request.env.user.partner_id
        result = empty
        # Savepoint + rollback: the rating order leaves no trace.
        try:
            with request.env.cr.savepoint():
                so_values = website._prepare_sale_order_values(partner)
                so_values["order_line"] = [
                    (0, 0, {"product_id": product.id, "product_uom_qty": quantity})
                ]
                order = request.env["sale.order"].sudo().create(so_values)
                currency = order.currency_id
                # No carrier for a service-only order (mirrors the gate in
                # `_get_express_shop_payment_values`): shipping the real cart
                # can't charge would diverge the total and Buckaroo rejects.
                methods = sorted(
                    self._get_delivery_methods_express_checkout(order).items(),
                    key=lambda item: item[1],
                ) if order._has_deliverable_products() else []
                result = {
                    "delivery_methods": [
                        {"id": carrier.id, "name": carrier.name, "amount": currency.round(price)}
                        for carrier, price in methods
                    ],
                    "subtotal": currency.round(order.amount_total - order.amount_delivery),
                    "currency_code": currency.name,
                }
                raise self._DiscardOrder()
        except self._DiscardOrder:
            pass
        return result

    @http.route(
        "/shop/buckaroo/wallet/set_method",
        type="jsonrpc",
        auth="public",
        methods=["POST"],
        website=True,
        sitemap=False,
    )
    def buckaroo_wallet_set_method(self, dm_id):
        order_sudo = request.cart
        if not order_sudo:
            return dict(self._EMPTY_AMOUNTS)
        try:
            cid = int(dm_id)
        except (TypeError, ValueError):
            return self._buckaroo_wallet_amounts()
        carrier = request.env["delivery.carrier"].sudo().browse(cid).exists()
        if carrier and carrier.id in order_sudo._get_delivery_methods().ids:
            order_sudo._set_delivery_method(carrier)
        return self._buckaroo_wallet_amounts()

    def _buckaroo_wallet_amounts(self):
        order_sudo = request.cart
        currency = order_sudo.currency_id
        total = order_sudo.amount_total
        return {
            "amount": total,
            "minor_amount": payment_utils.to_minor_currency_units(total, currency),
            "currency_code": currency.name,
        }
