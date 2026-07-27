# Part of Odoo. See LICENSE file for full copyright and licensing details.

from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.fields import Command
from odoo.tests import tagged

from odoo.addons.payment_buckaroo_official.tests.common import BuckarooOfficialCommon


@tagged("post_install", "-at_install")
class TestAccountMoveBuckarooRefund(BuckarooOfficialCommon):
    @classmethod
    def _make_buckaroo_tx(cls, reference, amount=50.0):
        return cls.env["payment.transaction"].create(
            {
                "provider_id": cls.buckaroo.id,
                "payment_method_id": cls.ideal.id,
                "reference": reference,
                "amount": amount,
                "currency_id": cls.currency_euro.id,
                "partner_id": cls.partner.id,
                "operation": "online_redirect",
            }
        )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner.write(
            {
                "property_account_receivable_id": cls.env["account.account"]
                .search(
                    [("account_type", "=", "asset_receivable")],
                    limit=1,
                )
                .id,
            }
        )

        cls.invoice = cls.env["account.move"].create(
            {
                "move_type": "out_invoice",
                "partner_id": cls.partner.id,
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": "Test",
                            "quantity": 1,
                            "price_unit": 50.0,
                        }
                    )
                ],
            }
        )
        cls.invoice.action_post()

        cls.tx = cls._make_buckaroo_tx("ACCMOVE-TX-1")
        cls.tx.invoice_ids = [Command.link(cls.invoice.id)]
        cls.tx._set_done()
        cls.tx._post_process()

        cls.credit_note = cls.invoice._reverse_moves(cancel=True)

    def test_compute_valid_for_refund_on_credit_note(self):
        self.assertTrue(self.credit_note.valid_for_buckaroo_official_refund)

    def test_compute_invalid_on_invoice(self):
        self.assertFalse(self.invoice.valid_for_buckaroo_official_refund)

    def test_compute_invalid_on_draft_credit_note(self):
        draft = self.invoice._reverse_moves()
        self.assertFalse(draft.valid_for_buckaroo_official_refund)

    def test_compute_invalid_for_non_buckaroo_invoice(self):
        other_invoice = self.env["account.move"].create(
            {
                "move_type": "out_invoice",
                "partner_id": self.partner.id,
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": "Test",
                            "quantity": 1,
                            "price_unit": 10.0,
                        }
                    )
                ],
            }
        )
        other_invoice.action_post()
        other_credit = other_invoice._reverse_moves(cancel=True)
        self.assertFalse(other_credit.valid_for_buckaroo_official_refund)

    def test_action_returns_refund_wizard_with_payment_context(self):
        action = self.credit_note.action_buckaroo_official_refund()
        self.assertEqual(action["res_model"], "payment.refund.wizard")
        self.assertEqual(action["context"]["active_id"], self.tx.payment_id.id)

    def test_wizard_initialises_with_correct_payment_and_amount(self):
        action = self.credit_note.action_buckaroo_official_refund()
        wizard = self.env["payment.refund.wizard"].with_context(**action["context"]).create({})
        self.assertEqual(wizard.payment_id, self.tx.payment_id)
        self.assertEqual(wizard.transaction_id, self.tx)
        self.assertEqual(wizard.amount_available_for_refund, self.tx.amount)
        self.assertEqual(wizard.amount_to_refund, self.tx.amount)
        self.assertEqual(wizard.support_refund, "partial")

    def test_wizard_rejects_amount_above_available(self):
        action = self.credit_note.action_buckaroo_official_refund()
        wizard = self.env["payment.refund.wizard"].with_context(**action["context"]).create({})
        with self.assertRaises(ValidationError):
            wizard.amount_to_refund = self.tx.amount + 1.0

    def _add_second_buckaroo_tx(self, reference="ACCMOVE-TX-2", amount=50.0, payment_method=None):
        pm = payment_method or self.ideal
        extra_tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": pm.id,
                "reference": reference,
                "amount": amount,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
            }
        )
        extra_tx.invoice_ids = [Command.link(self.invoice.id)]
        extra_tx._set_done()
        extra_tx._post_process()
        return extra_tx

    def _make_giftcard_tx_for_invoice(self, reference="ACCMOVE-GC-1", amount=50.0):
        giftcard = self.env.ref("payment_buckaroo_official.payment_method_brand_vvvgiftcard")
        self.buckaroo.payment_method_ids = [Command.link(giftcard.id)]
        return self._add_second_buckaroo_tx(
            reference=reference, amount=amount, payment_method=giftcard
        )

    def test_action_returns_notification_for_multi_giftcard_transactions(self):
        self._make_giftcard_tx_for_invoice()
        called_on = []

        def fake_send(self):
            called_on.append(self.source_transaction_id)

        with patch.object(type(self.env["payment.transaction"]), "_send_refund_request", fake_send):
            action = self.credit_note.action_buckaroo_official_refund()
        self.assertEqual(action["type"], "ir.actions.client")
        self.assertEqual(action["tag"], "display_notification")

    def test_action_raises_for_multi_non_giftcard_transactions(self):
        """No giftcard among the txns → raise. Two creditcard payments on
        one order must be refunded individually so the admin chooses which
        leg is debited."""
        self._add_second_buckaroo_tx()
        with self.assertRaises(UserError):
            self.credit_note.action_buckaroo_official_refund()

    def test_multi_tx_refund_total_matches_cn_amount(self):
        """Greedy distribution across giftcard + remainder: total refund
        equals CN amount, not the sum of source tx amounts."""
        self._make_giftcard_tx_for_invoice()
        refund_amounts = []

        def fake_send(self):
            refund_amounts.append(abs(self.amount))

        with patch.object(type(self.env["payment.transaction"]), "_send_refund_request", fake_send):
            self.credit_note.action_buckaroo_official_refund()
        self.assertAlmostEqual(
            sum(refund_amounts),
            abs(self.credit_note.amount_total),
            places=2,
        )

    def test_existing_refund_tx_does_not_count_as_multi(self):
        refund_tx = self.tx._create_child_transaction(self.tx.amount, is_refund=True)
        refund_tx._set_done()
        txs = self.credit_note._find_valid_buckaroo_official_transactions()
        self.assertEqual(txs, self.tx)
        self.assertNotIn(refund_tx, txs)

    def test_find_via_sale_line_when_reversed_entry_missing(self):
        if "sale_line_ids" not in self.env["account.move.line"]._fields:
            self.skipTest("sale module not installed")
        sale_order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": [
                    Command.create(
                        {
                            "name": "Test",
                            "product_id": self.env["product.product"].create({"name": "P"}).id,
                            "product_uom_qty": 1,
                            "price_unit": 50.0,
                        }
                    )
                ],
            }
        )
        sale_order.action_confirm()
        sale_tx = self._make_buckaroo_tx("SO-TX-1")
        sale_tx.sale_order_ids = [Command.link(sale_order.id)]
        sale_tx._set_done()
        sale_tx._post_process()

        invoice = sale_order._create_invoices()
        invoice.action_post()
        standalone_credit = self.env["account.move"].create(
            {
                "move_type": "out_refund",
                "partner_id": self.partner.id,
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": "Return",
                            "quantity": 1,
                            "price_unit": 50.0,
                            "sale_line_ids": [Command.set(sale_order.order_line.ids)],
                        }
                    )
                ],
            }
        )
        txs = standalone_credit._find_valid_buckaroo_official_transactions()
        self.assertIn(sale_tx, txs)

    def _fully_refund_source_payment(self):
        refund_tx = self.tx._create_child_transaction(self.tx.amount, is_refund=True)
        refund_tx._set_done()
        self.env["account.payment"].create(
            {
                "amount": self.tx.amount,
                "payment_type": "outbound",
                "partner_type": "customer",
                "partner_id": self.partner.id,
                "currency_id": self.tx.currency_id.id,
                "payment_transaction_id": refund_tx.id,
            }
        )
        self.tx.payment_id.invalidate_recordset(["amount_available_for_refund"])
        self.credit_note.invalidate_recordset(["valid_for_buckaroo_official_refund"])

    def test_compute_invalid_when_fully_refunded(self):
        self._fully_refund_source_payment()
        self.assertFalse(self.credit_note.valid_for_buckaroo_official_refund)

    def test_action_raises_when_fully_refunded(self):
        self._fully_refund_source_payment()
        with self.assertRaises(UserError) as cm:
            self.credit_note.action_buckaroo_official_refund()
        self.assertIn("fully refunded", str(cm.exception))
