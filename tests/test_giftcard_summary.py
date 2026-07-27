# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Tests for the Buckaroo giftcard order-summary customizations.

Covers:

- ``Paid with {brand name}`` lines rendered for each done giftcard
  ``payment.transaction`` on the order.
- ``Remaining Amount`` line shown when ``amount_paid < amount_total``.
- ``Already Paid`` line shown when ``amount_paid > 0``.
- ``website._get_and_cache_current_cart()`` preserves the cart when a
  Buckaroo giftcard partial payment is pending the remainder.
- Portal ``shop_payment_transaction`` computes ``amount = total - paid``
  when a prior giftcard payment exists on the order.
"""

from unittest.mock import MagicMock, patch

from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon


@tagged("post_install", "-at_install")
class TestGiftcardSummaryTemplate(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.giftcard = cls.env.ref("payment_buckaroo_official.payment_method_giftcard")
        cls.brand_vvv = cls.env.ref("payment_buckaroo_official.payment_method_brand_vvvgiftcard")
        cls.buckaroo.payment_method_ids = [
            Command.link(cls.giftcard.id),
            Command.link(cls.brand_vvv.id),
        ]
        cls.product = cls.env["product.product"].create(
            {
                "name": "Test Product",
                "list_price": 22.0,
                "type": "service",
            }
        )

    def _make_order(self, price=22.0):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "currency_id": self.currency_euro.id,
            }
        )
        self.env["sale.order.line"].create(
            {
                "order_id": order.id,
                "product_id": self.product.id,
                "product_uom_qty": 1,
                "price_unit": price,
                "tax_ids": [Command.clear()],
            }
        )
        return order

    def _add_done_giftcard_tx(self, order, amount, payment_method=None):
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": (payment_method or self.brand_vvv).id,
                "reference": f"TX-GC-{amount}",
                "amount": amount,
                "currency_id": order.currency_id.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
                "state": "done",
                "sale_order_ids": [Command.link(order.id)],
            }
        )
        return tx

    def _render_total(self, order):
        view = self.env.ref("website_sale.total")
        # ``hide_promotions=True`` skips the coupon_form sub-template, which
        # uses ``request.csrf_token()`` and crashes under test rendering.
        return view._render_template(
            view.id,
            {
                "website_sale_order": order.sudo(),
                "hide_promotions": True,
            },
        )

    def test_paid_with_brand_line_appears_for_done_giftcard_tx(self):
        order = self._make_order(price=22.0)
        self._add_done_giftcard_tx(order, amount=10.0)

        html = self._render_total(order)

        self.assertIn("Paid with", html)
        self.assertIn(self.brand_vvv.name, html)

    def test_remaining_amount_line_appears_when_partial_paid(self):
        order = self._make_order(price=22.0)
        self._add_done_giftcard_tx(order, amount=10.0)

        html = self._render_total(order)

        self.assertIn("Remaining Amount", html)

    def test_already_paid_line_appears_when_paid_greater_than_zero(self):
        order = self._make_order(price=22.0)
        self._add_done_giftcard_tx(order, amount=10.0)

        html = self._render_total(order)

        self.assertIn("Already Paid", html)

    def test_summary_omits_remaining_and_already_paid_when_no_payment(self):
        order = self._make_order(price=22.0)

        html = self._render_total(order)

        self.assertNotIn("Remaining Amount", html)
        self.assertNotIn("Already Paid", html)
        self.assertNotIn("Paid with", html)

    def test_paid_with_line_skipped_for_non_giftcard_tx(self):
        order = self._make_order(price=22.0)
        # Done tx, but for iDEAL (not giftcard) — no "Paid with iDEAL" line.
        self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.ideal.id,
                "reference": "TX-IDEAL-DONE",
                "amount": 22.0,
                "currency_id": order.currency_id.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
                "state": "done",
                "sale_order_ids": [Command.link(order.id)],
            }
        )

        html = self._render_total(order)

        self.assertNotIn("Paid with", html)

    def test_partial_giftcard_pending_remainder_is_truthy_for_partial_paid(self):
        order = self._make_order(price=22.0)
        self._add_done_giftcard_tx(order, amount=10.0)

        self.assertTrue(order._buckaroo_has_giftcard_pending_remainder())

    def test_partial_giftcard_pending_remainder_is_false_when_fully_paid(self):
        order = self._make_order(price=22.0)
        self._add_done_giftcard_tx(order, amount=22.0)

        self.assertFalse(order._buckaroo_has_giftcard_pending_remainder())

    def test_partial_giftcard_pending_remainder_is_false_when_no_tx(self):
        order = self._make_order(price=22.0)

        self.assertFalse(order._buckaroo_has_giftcard_pending_remainder())

    def test_partial_giftcard_pending_remainder_is_false_for_non_giftcard_tx(self):
        order = self._make_order(price=22.0)
        # Done iDEAL tx with partial amount — not a giftcard partial.
        self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.ideal.id,
                "reference": "TX-IDEAL-PARTIAL",
                "amount": 10.0,
                "currency_id": order.currency_id.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
                "state": "done",
                "sale_order_ids": [Command.link(order.id)],
            }
        )

        self.assertFalse(order._buckaroo_has_giftcard_pending_remainder())

    def test_website_get_cart_restores_partial_giftcard_order_after_super_reset(self):
        """End-to-end: when super resets the session, the override re-reads
        the cached id, sees the giftcard partial, and rebinds the cart."""
        from odoo.addons.website_sale.models.website import (
            CART_SESSION_CACHE_KEY,
        )

        order = self._make_order(price=22.0)
        self._add_done_giftcard_tx(order, amount=10.0)

        website = self.env["website"].search([], limit=1) or self.env["website"].create(
            {"name": "Buckaroo Test Website"}
        )
        session = {CART_SESSION_CACHE_KEY: order.id}
        fake_request = MagicMock()
        fake_request.session = session

        # Stub the parent class method with a function that mimics upstream:
        # pops the session key and returns an empty recordset.
        empty_so = self.env["sale.order"].sudo()

        def fake_super(self_inner):
            session.pop(CART_SESSION_CACHE_KEY, None)
            return empty_so

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_giftcard.request",
                fake_request,
            ),
            patch(
                "odoo.addons.website_sale.models.website.Website._get_and_cache_current_cart",
                fake_super,
            ),
        ):
            result = website._get_and_cache_current_cart()

        self.assertEqual(result, order)
        self.assertEqual(session.get(CART_SESSION_CACHE_KEY), order.id)

    def _make_draft_tx(self, order, amount, payment_method=None):
        return self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": (payment_method or self.ideal).id,
                "reference": f"TX-DRAFT-{amount}",
                "amount": amount,
                "currency_id": order.currency_id.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
                "state": "draft",
                "sale_order_ids": [Command.link(order.id)],
            }
        )

    def _invoke_validate_hook(self, order, draft_tx):
        from odoo.addons.payment_buckaroo_official.controllers.giftcard import (
            GiftcardPaymentPortal,
        )

        controller = GiftcardPaymentPortal()
        controller._validate_transaction_for_order(draft_tx, order)

    def test_validate_hook_shrinks_draft_tx_amount_to_remainder_when_partial_paid(self):
        order = self._make_order(price=22.0)
        self._add_done_giftcard_tx(order, amount=10.0)
        # A new draft iDEAL tx the upstream would have just created at full total.
        draft_tx = self._make_draft_tx(order, amount=22.0, payment_method=self.ideal)

        self._invoke_validate_hook(order, draft_tx)

        self.assertEqual(draft_tx.amount, 12.0)

    def test_validate_hook_leaves_tx_alone_when_nothing_paid(self):
        order = self._make_order(price=22.0)
        draft_tx = self._make_draft_tx(order, amount=22.0, payment_method=self.ideal)

        self._invoke_validate_hook(order, draft_tx)

        self.assertEqual(draft_tx.amount, 22.0)

    def test_validate_hook_leaves_tx_alone_when_already_remainder(self):
        order = self._make_order(price=22.0)
        self._add_done_giftcard_tx(order, amount=10.0)
        # Tx already at the remainder amount — hook is a no-op.
        draft_tx = self._make_draft_tx(order, amount=12.0, payment_method=self.ideal)

        self._invoke_validate_hook(order, draft_tx)

        self.assertEqual(draft_tx.amount, 12.0)

    def test_validate_hook_does_not_touch_pending_tx(self):
        order = self._make_order(price=22.0)
        self._add_done_giftcard_tx(order, amount=10.0)
        # A pending tx (not draft) shouldn't be mutated — hook is meant for
        # the freshly-created draft only.
        pending_tx = self._make_draft_tx(order, amount=22.0, payment_method=self.ideal)
        pending_tx.state = "pending"

        self._invoke_validate_hook(order, pending_tx)

        self.assertEqual(pending_tx.amount, 22.0)

    def test_paid_with_line_skipped_for_draft_giftcard_tx(self):
        order = self._make_order(price=22.0)
        # Pending (not done) giftcard tx — no "Paid with" line.
        self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.brand_vvv.id,
                "reference": "TX-GC-PENDING",
                "amount": 10.0,
                "currency_id": order.currency_id.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
                "state": "draft",
                "sale_order_ids": [Command.link(order.id)],
            }
        )

        html = self._render_total(order)

        self.assertNotIn("Paid with", html)


@tagged("post_install", "-at_install")
class TestGiftcardInlineRemainderLeg(TestGiftcardSummaryTemplate):
    """The on-site remainder leg is shrunk to the still-open amount and left as
    an INDEPENDENT payment — no ``source_transaction_id`` link to the giftcard
    slice. Both legs reconcile and refund on their own PBNK; the giftcard
    grouping lives on Buckaroo's side via the group transaction key.
    """

    def test_inline_remainder_leg_amount_shrunk_to_remainder(self):
        order = self._make_order(price=22.0)
        self._add_done_giftcard_tx(order, amount=10.0)
        # Upstream creates the next leg at the full order total.
        leg = self._make_draft_tx(order, amount=22.0, payment_method=self.ideal)

        self._invoke_validate_hook(order, leg)

        self.assertEqual(leg.amount, 12.0)

    def test_inline_remainder_leg_is_not_linked_to_giftcard(self):
        order = self._make_order(price=22.0)
        self._add_done_giftcard_tx(order, amount=10.0)
        leg = self._make_draft_tx(order, amount=12.0, payment_method=self.ideal)

        self._invoke_validate_hook(order, leg)

        self.assertFalse(leg.source_transaction_id)

    def test_second_giftcard_leg_is_not_linked(self):
        order = self._make_order(price=22.0)
        self._add_done_giftcard_tx(order, amount=10.0)
        leg = self._make_draft_tx(order, amount=12.0, payment_method=self.brand_vvv)

        self._invoke_validate_hook(order, leg)

        self.assertFalse(leg.source_transaction_id)

    def test_hook_leaves_amount_alone_when_nothing_paid(self):
        order = self._make_order(price=22.0)
        leg = self._make_draft_tx(order, amount=22.0, payment_method=self.ideal)

        self._invoke_validate_hook(order, leg)

        self.assertEqual(leg.amount, 22.0)
        self.assertFalse(leg.source_transaction_id)
