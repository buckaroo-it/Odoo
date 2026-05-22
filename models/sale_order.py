# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, fields, models
from odoo.fields import Command

from ..utils import const


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    is_buckaroo_surcharge = fields.Boolean(
        string="Is Buckaroo Surcharge",
        default=False,
        help="Marks this line as the Buckaroo payment surcharge so it can be "
        "located and replaced when the customer changes payment method.",
    )

    def _show_in_cart(self):
        self.ensure_one()
        return not self.is_buckaroo_surcharge and super()._show_in_cart()


class SaleOrder(models.Model):
    _inherit = "sale.order"

    amount_buckaroo_surcharge = fields.Monetary(
        string="Buckaroo Surcharge Amount",
        compute="_compute_amount_buckaroo_surcharge",
        store=True,
    )

    @api.depends("order_line.price_total", "order_line.is_buckaroo_surcharge")
    def _compute_amount_buckaroo_surcharge(self):
        for order in self:
            lines = order.order_line.filtered("is_buckaroo_surcharge")
            order.amount_buckaroo_surcharge = sum(lines.mapped("price_total"))

    def _apply_buckaroo_surcharge_line(self, payment_method):
        self.ensure_one()
        siblings = self.order_line.filtered(
            lambda l: not l.is_buckaroo_surcharge and not l.is_delivery
        )
        amount = payment_method._buckaroo_compute_surcharge_amount(
            sum(siblings.mapped("price_subtotal")),
            self.currency_id,
        )
        if not amount:
            self._remove_buckaroo_surcharge_line()
            return

        # Inherit VAT from sibling product line (Art. 73 VAT Directive).
        sibling_taxes = siblings[:1].tax_ids
        values = {
            "name": payment_method._buckaroo_get_surcharge_line_name(),
            "product_uom_qty": 1.0,
            "price_unit": amount,
            "tax_ids": [Command.set(sibling_taxes.ids)] if sibling_taxes else [Command.clear()],
        }
        existing = self.order_line.filtered("is_buckaroo_surcharge")
        if existing:
            existing.write(values)
            return

        product = self.env.ref("payment_buckaroo_official.product_buckaroo_surcharge")
        self.env["sale.order.line"].create(
            {
                "order_id": self.id,
                "product_id": product.id,
                "is_buckaroo_surcharge": True,
                **values,
            }
        )

    def _remove_buckaroo_surcharge_line(self):
        self.ensure_one()
        self.order_line.filtered("is_buckaroo_surcharge").unlink()

    def _buckaroo_sync_surcharge_for_method(self, payment_method):
        self.ensure_one()
        if not (payment_method and payment_method._is_linked_to_buckaroo()):
            self._remove_buckaroo_surcharge_line()
            return
        self._apply_buckaroo_surcharge_line(payment_method)

    def _buckaroo_partial_payment_remainder(self):
        """``amount_total - amount_paid`` when partly paid (>0 paid, < total);
        ``0.0`` otherwise. Single source of truth for the partial-payment
        predicate used in controllers, sale_order helpers, and QWeb."""
        self.ensure_one()
        currency = self.currency_id
        if currency.compare_amounts(self.amount_paid, 0) <= 0:
            return 0.0
        if currency.compare_amounts(self.amount_paid, self.amount_total) >= 0:
            return 0.0
        return self.amount_total - self.amount_paid

    def _check_cart_is_ready_to_be_paid(self):
        # Runs under parent's FOR NO KEY UPDATE NOWAIT; concurrent picks serialise.
        pm_id = self.env.context.get(const.PICK_METHOD_CONTEXT_KEY)
        if pm_id:
            pm = self.env["payment.method"].sudo().browse(pm_id).exists()
            if pm:
                for order in self:
                    order._buckaroo_sync_surcharge_for_method(pm)
        return super()._check_cart_is_ready_to_be_paid()
