# Part of Odoo. See LICENSE file for full copyright and licensing details.

from contextlib import contextmanager

from odoo import SUPERUSER_ID, _
from odoo.exceptions import UserError
from odoo.http import request


class BuckarooWalletExpressMixin:
    """Shared express-checkout cart logic for Buckaroo wallet buttons.

    Apple Pay and Google Pay both add a product to the cart from the
    product page and return the same JSON-safe express payload. Each
    wallet subclasses this alongside ``Cart`` and exposes its own thin
    route.
    """

    # Shared with `express_checkout.py` so producer and route can't drift.
    EXPRESS_CHECKOUT_ROUTE = "/shop/buckaroo/wallet/express_checkout"
    EXPRESS_ORDER_SESSION_KEY = "buckaroo_express_order_id"

    @staticmethod
    @contextmanager
    def _buckaroo_preserve_cart_badge():
        # Operating on the express order via `request.cart` makes core cache the
        # express order's qty as the header cart count; restore the shopper's.
        key = "website_sale_cart_quantity"
        prior = request.session.get(key)
        try:
            yield
        finally:
            if prior is None:
                request.session.pop(key, None)
            else:
                request.session[key] = prior

    def _get_express_shop_payment_values(self, order, **kwargs):
        # Preselect cheapest carrier so the sheet shows the final total. Rate
        # once and forward it; `_set_delivery_method` would otherwise re-rate
        # (a carrier API round-trip).
        if order._has_deliverable_products() and not order.carrier_id:
            rated = [(m, m.rate_shipment(order)) for m in order._get_delivery_methods()]
            if rated:
                cheapest, rate = min(rated, key=lambda mr: mr[1].get("price", float("inf")))
                order._set_delivery_method(cheapest, rate=rate)
        return super()._get_express_shop_payment_values(order, **kwargs)

    def _buckaroo_wallet_express_init(
        self,
        product_id=None,
        qty=None,
        no_variant_attribute_value_ids=None,
        product_custom_attribute_values=None,
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

        product = request.env["product.product"].browse(pid).exists()
        if not product:
            raise UserError(_("The given product does not exist."))

        add_kwargs = {}
        if no_variant_attribute_value_ids is not None:
            add_kwargs["no_variant_attribute_value_ids"] = no_variant_attribute_value_ids
        if product_custom_attribute_values is not None:
            add_kwargs["product_custom_attribute_values"] = product_custom_attribute_values

        # Buy-now: a dedicated order holding only this product, so the charge
        # matches the sheet and the shopper's cart is left untouched.
        website = request.website
        with self._buckaroo_preserve_cart_badge():
            request.cart = (
                request.env["sale.order"]
                .with_user(SUPERUSER_ID)
                .with_company(website.company_id)
                .create(website._prepare_sale_order_values(request.env.user.partner_id))
                .with_user(request.env.user)
                .sudo()
            )
            self.add_to_cart(
                product_template_id=product.product_tmpl_id.id,
                product_id=pid,
                quantity=quantity,
                **add_kwargs,
            )
            order_sudo = request.cart
            raw = self._get_express_shop_payment_values(order_sudo)
        request.session[self.EXPRESS_ORDER_SESSION_KEY] = order_sudo.id
        currency = raw.get("currency") or order_sudo.currency_id
        # Whitelist JSON-safe scalars; raw contains recordsets.
        return {
            "amount": raw.get("amount"),
            "minor_amount": raw.get("minor_amount"),
            "currency_code": currency.name,
            "partner_id": raw.get("partner_id"),
            "transaction_route": raw.get("transaction_route"),
            "express_checkout_route": self.EXPRESS_CHECKOUT_ROUTE,
            "shipping_info_required": bool(raw.get("shipping_info_required")),
            "landing_route": raw.get("landing_route") or "/shop/payment/validate",
            "access_token": raw.get("payment_access_token"),
        }
