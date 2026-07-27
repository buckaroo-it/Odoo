# Part of Odoo. See LICENSE file for full copyright and licensing details.

from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon


VALID_FEE_AMOUNTS = ["", "0", "1.50", "1%", "0.5%"]
INVALID_FEE_AMOUNTS = ["-1", "abc", "1.5%%", "%1", "1,50"]


class _SurchargeBase(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bancontact = cls.env.ref("payment_buckaroo_official.payment_method_bancontact")
        cls.buckaroo.payment_method_ids = [Command.link(cls.bancontact.id)]
        for method in (cls.ideal, cls.bancontact):
            method.write(
                {
                    "supported_country_ids": [Command.clear()],
                    "supported_currency_ids": [Command.clear()],
                }
            )
        cls.surcharge_product = cls.env.ref("payment_buckaroo_official.product_buckaroo_surcharge")
        # Service product: passes parent's only_services=True carrier check.
        cls.product = cls.env["product.product"].create(
            {
                "name": "Test Widget",
                "list_price": 100.0,
                "type": "service",
            }
        )

    def _make_order(self, qty=1, price=100.0):
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
                "product_uom_qty": qty,
                "price_unit": price,
                "tax_ids": [Command.clear()],
            }
        )
        return order

    def _surcharge_lines(self, order):
        return order.order_line.filtered("is_buckaroo_surcharge")


@tagged("post_install", "-at_install")
class TestSurchargeFieldValidation(BuckarooOfficialCommon):
    def test_accepts_valid_values(self):
        for value in VALID_FEE_AMOUNTS:
            with self.subTest(value=value):
                self.ideal.write({"buckaroo_official_fee_amount": value})
                self.assertEqual(self.ideal.buckaroo_official_fee_amount, value)

    def test_rejects_invalid_values(self):
        for value in INVALID_FEE_AMOUNTS:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.ideal.write({"buckaroo_official_fee_amount": value})

    def test_accepts_whitespace_padding(self):
        self.ideal.write({"buckaroo_official_fee_amount": " 1.50 "})
        self.assertEqual(
            self.ideal._buckaroo_compute_surcharge_amount(100.0, self.currency_euro),
            1.5,
        )


@tagged("post_install", "-at_install")
class TestSurchargeCompute(BuckarooOfficialCommon):
    def test_compute_fixed_and_percent(self):
        for value, subtotal, expected in [
            ("1.50", 100.0, 1.5),
            ("1%", 100.0, 1.0),
            ("1%", 33.33, 0.33),
        ]:
            with self.subTest(value=value):
                self.ideal.buckaroo_official_fee_amount = value
                self.assertEqual(
                    self.ideal._buckaroo_compute_surcharge_amount(subtotal, self.currency_euro),
                    expected,
                )

    def test_compute_zero_for_empty_or_zero_input(self):
        for value in ("", "0", "0%"):
            with self.subTest(value=value):
                self.ideal.buckaroo_official_fee_amount = value
                self.assertEqual(
                    self.ideal._buckaroo_compute_surcharge_amount(100.0, self.currency_euro),
                    0.0,
                )

    def test_line_name_uses_method_name(self):
        self.ideal.name = "iDEAL"
        self.assertEqual(self.ideal._buckaroo_get_surcharge_line_name(), "iDEAL surcharge")

    def test_label_suffix_empty_for_zero_or_no_currency(self):
        for value, currency in [
            ("", self.currency_euro),
            ("0", self.currency_euro),
            ("1.50", self.env["res.currency"]),
        ]:
            with self.subTest(value=value, currency=currency):
                self.ideal.buckaroo_official_fee_amount = value
                self.assertEqual(self.ideal._buckaroo_get_label_suffix(currency), "")

    def test_label_suffix_percent(self):
        for value, expected in [("1%", " (+ 1%)"), ("0.5%", " (+ 0.5%)")]:
            with self.subTest(value=value):
                self.ideal.buckaroo_official_fee_amount = value
                self.assertEqual(
                    self.ideal._buckaroo_get_label_suffix(self.currency_euro),
                    expected,
                )

    def test_label_suffix_fixed_includes_currency(self):
        self.ideal.buckaroo_official_fee_amount = "1.50"
        suffix = self.ideal._buckaroo_get_label_suffix(self.currency_euro)
        self.assertIn("1.50", suffix)
        self.assertIn(self.currency_euro.symbol, suffix)
        self.assertTrue(suffix.startswith(" (+ ") and suffix.endswith(")"))


@tagged("post_install", "-at_install")
class TestSurchargeOrderLine(_SurchargeBase):
    def test_apply_adds_taxless_line(self):
        self.ideal.buckaroo_official_fee_amount = "1.50"
        order = self._make_order()

        order._apply_buckaroo_surcharge_line(self.ideal)

        lines = self._surcharge_lines(order)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.product_id, self.surcharge_product)
        self.assertEqual(lines.price_unit, 1.5)
        self.assertEqual(lines.product_uom_qty, 1.0)
        self.assertEqual(lines.name, f"{self.ideal.name} surcharge")
        self.assertFalse(lines.tax_ids)

    def test_apply_twice_is_idempotent(self):
        self.ideal.buckaroo_official_fee_amount = "1.50"
        order = self._make_order()
        order._apply_buckaroo_surcharge_line(self.ideal)
        order._apply_buckaroo_surcharge_line(self.ideal)
        self.assertEqual(len(self._surcharge_lines(order)), 1)

    def test_apply_with_different_method_replaces(self):
        self.ideal.buckaroo_official_fee_amount = "1.50"
        self.bancontact.buckaroo_official_fee_amount = "2.00"
        order = self._make_order()
        order._apply_buckaroo_surcharge_line(self.ideal)
        order._apply_buckaroo_surcharge_line(self.bancontact)

        lines = self._surcharge_lines(order)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.price_unit, 2.0)
        self.assertEqual(lines.name, f"{self.bancontact.name} surcharge")

    def test_remove_deletes_line(self):
        self.ideal.buckaroo_official_fee_amount = "1.50"
        order = self._make_order()
        order._apply_buckaroo_surcharge_line(self.ideal)
        order._remove_buckaroo_surcharge_line()
        self.assertEqual(len(self._surcharge_lines(order)), 0)

    def test_apply_zero_fee_removes_existing(self):
        self.ideal.buckaroo_official_fee_amount = "1.50"
        order = self._make_order()
        order._apply_buckaroo_surcharge_line(self.ideal)
        self.ideal.buckaroo_official_fee_amount = "0"
        order._apply_buckaroo_surcharge_line(self.ideal)
        self.assertEqual(len(self._surcharge_lines(order)), 0)

    def test_percent_surcharge_does_not_compound(self):
        self.ideal.buckaroo_official_fee_amount = "1%"
        order = self._make_order(price=100.0)

        order._apply_buckaroo_surcharge_line(self.ideal)
        self.assertEqual(self._surcharge_lines(order).price_unit, 1.0)

        order._apply_buckaroo_surcharge_line(self.ideal)
        self.assertEqual(self._surcharge_lines(order).price_unit, 1.0)


@tagged("post_install", "-at_install")
class TestSurchargeTaxInheritance(_SurchargeBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tax_21 = cls.env["account.tax"].create(
            {
                "name": "VAT 21%",
                "type_tax_use": "sale",
                "amount_type": "percent",
                "amount": 21.0,
            }
        )
        cls.ideal.buckaroo_official_fee_amount = "1.50"

    def _make_taxed_order(self, price=100.0):
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
                "tax_ids": [Command.set(self.tax_21.ids)],
            }
        )
        return order

    def test_inherits_tax_from_product_line(self):
        order = self._make_taxed_order()
        order._apply_buckaroo_surcharge_line(self.ideal)
        self.assertEqual(self._surcharge_lines(order).tax_ids, self.tax_21)

    def test_total_includes_vat_on_surcharge(self):
        order = self._make_taxed_order(price=100.0)
        base_total = order.amount_total  # 100 + 21 VAT = 121
        order._apply_buckaroo_surcharge_line(self.ideal)
        # 1.50 + 21% = 1.815 → 1.82.
        self.assertAlmostEqual(order.amount_total, base_total + 1.82, places=2)

    def test_delivery_line_is_not_a_sibling(self):
        order = self._make_taxed_order()
        other_tax = self.env["account.tax"].create(
            {
                "name": "VAT 9%",
                "type_tax_use": "sale",
                "amount_type": "percent",
                "amount": 9.0,
            }
        )
        delivery_line = self.env["sale.order.line"].create(
            {
                "order_id": order.id,
                "product_id": self.product.id,
                "product_uom_qty": 1.0,
                "price_unit": 5.0,
                "tax_ids": [Command.set(other_tax.ids)],
            }
        )
        delivery_line.is_delivery = True

        order._apply_buckaroo_surcharge_line(self.ideal)

        self.assertEqual(self._surcharge_lines(order).tax_ids, self.tax_21)


@tagged("post_install", "-at_install")
class TestSurchargeSync(_SurchargeBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ideal.buckaroo_official_fee_amount = "1.50"
        cls.bancontact.buckaroo_official_fee_amount = "2.00"

    def test_pick_buckaroo_method_adds_surcharge(self):
        order = self._make_order()
        base_total = order.amount_total
        order._buckaroo_sync_surcharge_for_method(self.ideal)
        self.assertEqual(len(self._surcharge_lines(order)), 1)
        self.assertEqual(order.amount_total, base_total + 1.50)

    def test_switch_between_buckaroo_methods_replaces_line(self):
        order = self._make_order()
        order._buckaroo_sync_surcharge_for_method(self.ideal)
        order._buckaroo_sync_surcharge_for_method(self.bancontact)
        lines = self._surcharge_lines(order)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.price_unit, 2.00)

    def test_switch_to_non_buckaroo_or_empty_method_removes_surcharge(self):
        for method in (self.payment_method, self.env["payment.method"]):
            with self.subTest(method=method):
                order = self._make_order()
                order._buckaroo_sync_surcharge_for_method(self.ideal)
                order._buckaroo_sync_surcharge_for_method(method)
                self.assertEqual(len(self._surcharge_lines(order)), 0)


@tagged("post_install", "-at_install")
class TestSurchargeController(_SurchargeBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ideal.buckaroo_official_fee_amount = "1.50"

    def _line_snapshot(self, order):
        return [(l.product_id.id, l.product_uom_qty, l.price_unit) for l in order.order_line]

    def _build_kwargs(self, order, payment_method, **overrides):
        kwargs = {
            "payment_method_id": payment_method.id,
            "access_token": order._portal_ensure_token(),
            "amount": order.amount_total,
            "currency_id": order.currency_id.id,
            "partner_id": order.partner_id.id,
            "flow": "redirect",
            "tokenization_requested": False,
            "landing_route": "/shop/payment/validate",
            "provider_id": self.buckaroo.id,
            "token_id": None,
        }
        kwargs.update(overrides)
        return kwargs

    def _invoke_controller(self, order, payment_method, **extra):
        # Fake super() replays parent: token check + post-lock hook.
        from odoo.tools import consteq

        from odoo.addons.payment_buckaroo_official.controllers.fee import (
            BuckarooFeePaymentPortal,
        )
        from odoo.addons.payment_buckaroo_official.controllers import fee as _fee_module
        from odoo.addons.website_sale.controllers.payment import PaymentPortal

        captured = {}

        def fake_super_shop(self_inner, order_id, access_token, **kwargs):
            captured["order_id"] = order_id
            captured["access_token"] = access_token
            env = _fee_module.request.env
            order_sudo = env["sale.order"].sudo().browse(order_id)
            token_ok = (
                order_sudo
                and order_sudo.state in ("draft", "sent")
                and access_token
                and order_sudo.access_token
                and consteq(order_sudo.access_token, access_token)
            )
            if token_ok:
                order_sudo._check_cart_is_ready_to_be_paid()
            if not kwargs.get("amount"):
                kwargs["amount"] = order_sudo.amount_total
            captured["kwargs"] = kwargs
            return {"ok": True}

        kwargs = self._build_kwargs(order, payment_method)
        kwargs.update(extra)

        controller = BuckarooFeePaymentPortal()
        original = PaymentPortal.shop_payment_transaction
        PaymentPortal.shop_payment_transaction = fake_super_shop
        try:
            fake_request = MagicMock()
            fake_request.env = self.env

            def _update_context(**ctx):
                fake_request.env = fake_request.env(
                    context={
                        **fake_request.env.context,
                        **ctx,
                    }
                )

            fake_request.update_context.side_effect = _update_context
            with patch(
                "odoo.addons.payment_buckaroo_official.controllers.fee.request",
                fake_request,
            ):
                controller.shop_payment_transaction(
                    order.id,
                    kwargs.pop("access_token"),
                    **kwargs,
                )
        finally:
            PaymentPortal.shop_payment_transaction = original
        return captured

    def test_applies_surcharge_for_buckaroo_method(self):
        order = self._make_order()
        base_total = order.amount_total
        captured = self._invoke_controller(order, self.ideal)
        self.assertEqual(len(self._surcharge_lines(order)), 1)
        self.assertEqual(captured["kwargs"]["amount"], base_total + 1.50)
        self.assertEqual(order.amount_total, base_total + 1.50)

    def test_removes_surcharge_for_non_buckaroo_method(self):
        order = self._make_order()
        order._apply_buckaroo_surcharge_line(self.ideal)
        captured = self._invoke_controller(order, self.payment_method)
        self.assertEqual(len(self._surcharge_lines(order)), 0)
        self.assertEqual(captured["kwargs"]["amount"], order.amount_total)

    def test_tx_amount_equals_order_total_after_surcharge(self):
        order = self._make_order()
        captured = self._invoke_controller(order, self.ideal)
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.ideal.id,
                "reference": "TX-SURCHARGE-CHECK",
                "amount": captured["kwargs"]["amount"],
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
            }
        )
        self.assertEqual(tx.amount, order.amount_total)

    def test_buckaroo_token_applies_surcharge_for_token_method(self):
        order = self._make_order()
        base_total = order.amount_total
        token = self._create_token(
            provider_id=self.buckaroo.id,
            payment_method_id=self.ideal.id,
        )
        captured = self._invoke_controller(
            order,
            self.ideal,
            token_id=token.id,
            flow="token",
        )
        lines = self._surcharge_lines(order)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.price_unit, 1.50)
        self.assertEqual(captured["kwargs"]["amount"], base_total + 1.50)

    def test_non_buckaroo_token_removes_existing_surcharge(self):
        order = self._make_order()
        order._apply_buckaroo_surcharge_line(self.ideal)
        base_total = order.amount_total - 1.50
        token = self._create_token(
            provider_id=self.provider.id,
            payment_method_id=self.payment_method.id,
        )
        captured = self._invoke_controller(
            order,
            self.ideal,
            token_id=token.id,
            flow="token",
        )
        self.assertEqual(len(self._surcharge_lines(order)), 0)
        self.assertEqual(captured["kwargs"]["amount"], base_total)

    def test_invalid_access_token_does_not_mutate_order(self):
        order = self._make_order()
        order._portal_ensure_token()
        before = self._line_snapshot(order)
        self._invoke_controller(order, self.ideal, access_token="not-the-real-token")
        self.assertEqual(self._line_snapshot(order), before)
        self.assertFalse(self._surcharge_lines(order))

    def test_confirmed_order_does_not_mutate(self):
        order = self._make_order()
        order.write({"state": "sale"})
        before = self._line_snapshot(order)
        self._invoke_controller(order, self.ideal)
        self.assertEqual(self._line_snapshot(order), before)
        self.assertFalse(self._surcharge_lines(order))

    def test_client_amount_dropped_unconditionally(self):
        # Server-truth wins on every branch.
        for branch in (
            {"flow": "token", "token_id": 999},
            {"payment_method_id": None},
            {},
        ):
            with self.subTest(branch=branch):
                order = self._make_order()
                captured = self._invoke_controller(
                    order,
                    self.ideal,
                    amount=0.01,
                    **branch,
                )
                self.assertEqual(captured["kwargs"]["amount"], order.amount_total)

    def test_non_buckaroo_provider_amount_kwarg_preserved(self):
        """H2: the controller is global (extends ``website_sale.PaymentPortal``)
        so it runs for every provider. Without a gate, ``kwargs.pop('amount')``
        eats other providers' client-supplied amounts. The gate must keep
        non-Buckaroo amounts intact."""
        order = self._make_order()
        captured = self._invoke_controller(
            order,
            self.payment_method,
            provider_id=self.provider.id,
            amount=99.99,
        )
        self.assertEqual(captured["kwargs"]["amount"], 99.99)


@tagged("post_install", "-at_install")
class TestSurchargeSetMethodRoute(_SurchargeBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ideal.buckaroo_official_fee_amount = "1.50"

    def _invoke_set_method(self, order, **payload):
        from odoo.addons.payment_buckaroo_official.controllers.fee import (
            BuckarooFeePaymentPortal,
        )

        controller = BuckarooFeePaymentPortal()
        fake_request = MagicMock()
        fake_request.env = self.env
        fake_request.cart = order
        with patch(
            "odoo.addons.payment_buckaroo_official.controllers.fee.request",
            fake_request,
        ):
            return controller.buckaroo_official_set_method(**payload)

    def test_payment_method_id_applies_surcharge(self):
        order = self._make_order()
        result = self._invoke_set_method(order, payment_method_id=self.ideal.id)
        self.assertEqual(len(self._surcharge_lines(order)), 1)
        self.assertTrue(result["has_buckaroo_surcharge"])

    def test_buckaroo_token_id_applies_surcharge_for_token_method(self):
        order = self._make_order()
        token = self._create_token(
            provider_id=self.buckaroo.id,
            payment_method_id=self.ideal.id,
        )
        result = self._invoke_set_method(order, token_id=token.id)
        lines = self._surcharge_lines(order)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.price_unit, 1.50)
        self.assertTrue(result["has_buckaroo_surcharge"])

    def test_non_buckaroo_token_id_removes_existing_surcharge(self):
        order = self._make_order()
        order._apply_buckaroo_surcharge_line(self.ideal)
        token = self._create_token(
            provider_id=self.provider.id,
            payment_method_id=self.payment_method.id,
        )
        result = self._invoke_set_method(order, token_id=token.id)
        self.assertEqual(len(self._surcharge_lines(order)), 0)
        self.assertFalse(result["has_buckaroo_surcharge"])


@tagged("post_install", "-at_install")
class TestSurchargeRefund(_SurchargeBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ideal.buckaroo_official_fee_amount = "1.50"

    def _surcharge_invoice_lines(self, move):
        return move.invoice_line_ids.filtered(lambda l: l.product_id == self.surcharge_product)

    def test_full_refund_includes_surcharge(self):
        order = self._make_order()
        order._apply_buckaroo_surcharge_line(self.ideal)
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()

        original_lines = self._surcharge_invoice_lines(invoice)
        self.assertEqual(len(original_lines), 1)
        original_amount = abs(original_lines.price_subtotal)

        wizard = (
            self.env["account.move.reversal"]
            .with_context(
                active_model="account.move",
                active_ids=invoice.ids,
            )
            .create(
                {
                    "journal_id": invoice.journal_id.id,
                }
            )
        )
        wizard.reverse_moves()

        credit_note = wizard.new_move_ids
        self.assertEqual(credit_note.move_type, "out_refund")
        cn_lines = self._surcharge_invoice_lines(credit_note)
        self.assertEqual(len(cn_lines), 1)
        self.assertEqual(abs(cn_lines.price_subtotal), original_amount)
