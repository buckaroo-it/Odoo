# Part of Odoo. See LICENSE file for full copyright and licensing details.

import inspect
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon, make_mock_sdk_builder

_PAYPAL_PS = "odoo.addons.payment_buckaroo_official.models.payment_method_paypal.PaymentService"


@tagged("post_install", "-at_install")
class TestPaypalProductExpressInitController(BuckarooOfficialCommon):
    """Unit-tests for `/shop/buckaroo/paypal/express_init`.

    The route is a thin wrapper around the shared wallet express mixin
    (exhaustively tested via Apple Pay). These tests cover what is unique
    to the PayPal controller: its route shape, signature, and that it
    surfaces the mixin's input validation.
    """

    def _call_endpoint(
        self,
        product_id=1,
        qty=1,
        product_exists=True,
        express_payload=None,
    ):
        """Invoke the controller with a mocked `request`.

        Returns ``(result, add_to_cart_mock)``.
        """
        from ..controllers.express_paypal import BuckarooPaypalExpressController

        mock_cart = MagicMock()
        mock_cart.currency_id.name = "EUR"

        mock_product = MagicMock()
        mock_product.product_tmpl_id.id = 99
        mock_product.exists.return_value = mock_product if product_exists else []

        fake_env = MagicMock()
        fake_env.__getitem__.return_value.browse.return_value = mock_product
        fake_env.lang = "en_US"

        fake_req = MagicMock()
        fake_req.cart = mock_cart
        fake_req.env = fake_env

        controller = BuckarooPaypalExpressController()
        payload = (
            express_payload
            if express_payload is not None
            else {
                "amount": 12.34,
                "minor_amount": 1234,
                "currency": mock_cart.currency_id,
                "partner_id": 7,
                "transaction_route": "/shop/payment/transaction/99",
                "shipping_info_required": False,
            }
        )
        import odoo.http

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.express_wallet.request",
                new=fake_req,
            ),
            patch.object(odoo.http, "request", fake_req),
            patch.object(
                BuckarooPaypalExpressController,
                "add_to_cart",
                return_value={"cart_quantity": qty or 0},
            ) as add_to_cart_mock,
            patch.object(
                BuckarooPaypalExpressController,
                "_get_express_shop_payment_values",
                return_value=dict(payload),
            ),
        ):
            try:
                result = controller.paypal_express_init(product_id=product_id, qty=qty)
            except Exception as err:
                return err, add_to_cart_mock
            return result, add_to_cart_mock

    def test_route_shape_matches_applepay(self):
        from ..controllers.express_paypal import BuckarooPaypalExpressController

        routing = BuckarooPaypalExpressController.paypal_express_init.original_routing
        self.assertEqual(routing["routes"], ["/shop/buckaroo/paypal/express_init"])
        self.assertEqual(routing["type"], "jsonrpc")
        self.assertEqual(routing["auth"], "public")
        self.assertEqual(routing["methods"], ["POST"])

    def test_signature_excludes_dispatcher_dropped_args(self):
        from ..controllers.express_paypal import BuckarooPaypalExpressController

        sig = inspect.signature(BuckarooPaypalExpressController.paypal_express_init)
        self.assertNotIn("combination_id", sig.parameters)
        self.assertNotIn("product_template_id", sig.parameters)
        self.assertNotIn("kwargs", sig.parameters)

    def test_missing_product_id_raises_user_error(self):
        result, _ = self._call_endpoint(product_id=None)
        self.assertIsInstance(result, UserError)

    def test_non_positive_qty_raises_user_error(self):
        result, _ = self._call_endpoint(product_id=10, qty=0)
        self.assertIsInstance(result, UserError)

    def test_non_existent_product_raises_user_error(self):
        result, _ = self._call_endpoint(product_id=99999, product_exists=False)
        self.assertIsInstance(result, UserError)

    def test_valid_call_returns_express_payment_values_with_currency_code(self):
        result, add_to_cart_mock = self._call_endpoint(product_id=15, qty=2)
        self.assertNotIsInstance(result, Exception)
        add_to_cart_mock.assert_called_once()
        self.assertEqual(result.get("amount"), 12.34)
        self.assertEqual(result.get("currency_code"), "EUR")


@tagged("post_install", "-at_install")
class TestPaypalPaymentPortalSession(BuckarooOfficialCommon):
    """`PaypalPaymentPortal.shop_payment_transaction` stashes the client-side
    PayPal order id so `_buckaroo_create_payment` can pop it, and drops the
    stash if the transaction aborts before it is consumed."""

    def _call(
        self,
        *,
        pm_code="buckaroo_paypal",
        order_id="PP-ORDER-1",
        super_raises=False,
        super_result=None,
        express=None,
        fake_req=None,
        super_kwargs=None,
    ):
        from ..controllers.paypal import PaypalPaymentPortal
        from odoo.addons.website_sale.controllers.payment import PaymentPortal

        session = {}
        pm = MagicMock()
        pm.code = pm_code

        fake_env = MagicMock()
        fake_env.__getitem__.return_value.sudo.return_value.browse.return_value = pm

        fake_req = fake_req or MagicMock()
        fake_req.env = fake_env
        fake_req.session = session

        kwargs = {"payment_method_id": 5}
        if order_id is not None:
            kwargs["buckaroo_paypal_order_id"] = order_id
        if express is not None:
            kwargs["buckaroo_paypal_express"] = express

        def _super(self_, order_id_, access_token, **kw):
            if super_kwargs is not None:
                super_kwargs.update(kw)
            if super_raises:
                raise ValueError("boom")
            return super_result or {"ok": True}

        controller = PaypalPaymentPortal()
        with (
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.paypal.request",
                new=fake_req,
            ),
            patch.object(PaymentPortal, "shop_payment_transaction", new=_super),
        ):
            try:
                return controller.shop_payment_transaction(1, "tok", **kwargs), session
            except Exception as err:
                return err, session

    def test_happy_path_keeps_stash_for_create_payment_to_pop(self):
        result, session = self._call()
        self.assertEqual(result, {"ok": True})
        self.assertEqual(session.get("buckaroo_paypal_order_id"), "PP-ORDER-1")

    def test_abort_drops_stashed_order_id(self):
        result, session = self._call(super_raises=True)
        self.assertIsInstance(result, ValueError)
        self.assertNotIn("buckaroo_paypal_order_id", session)

    def test_non_paypal_method_does_not_stash(self):
        result, session = self._call(pm_code="buckaroo_ideal")
        self.assertEqual(result, {"ok": True})
        self.assertNotIn("buckaroo_paypal_order_id", session)

    def test_refusal_without_raising_drops_stashed_order_id(self):
        refused = {"state": "error", "state_message": "", "redirect": "/shop/address"}
        result, session = self._call(super_result=refused)
        self.assertEqual(result, refused)
        self.assertNotIn("buckaroo_paypal_order_id", session)

    def test_express_flag_sets_context_and_is_popped(self):
        fake_req = MagicMock()
        super_kwargs = {}
        self._call(express=True, fake_req=fake_req, super_kwargs=super_kwargs)
        fake_req.update_context.assert_called_once_with(buckaroo_paypal_express=True)
        self.assertNotIn("buckaroo_paypal_express", super_kwargs)

    def test_checkout_call_without_flag_sets_no_context(self):
        fake_req = MagicMock()
        self._call(fake_req=fake_req)
        fake_req.update_context.assert_not_called()

    def test_express_flag_must_be_a_real_true(self):
        for value in ("false", "true", 1):
            fake_req = MagicMock()
            super_kwargs = {}
            self._call(express=value, fake_req=fake_req, super_kwargs=super_kwargs)
            fake_req.update_context.assert_not_called()
            self.assertNotIn("buckaroo_paypal_express", super_kwargs)

    def test_express_flag_ignored_for_other_methods(self):
        """Left in kwargs for core's whitelist to reject; never sets the context."""
        fake_req = MagicMock()
        super_kwargs = {}
        self._call(
            pm_code="buckaroo_ideal", express=True, fake_req=fake_req, super_kwargs=super_kwargs
        )
        fake_req.update_context.assert_not_called()
        self.assertTrue(super_kwargs.get("buckaroo_paypal_express"))

    def test_checkout_transaction_route_call_stash_is_consumable_by_create_payment(self):
        """The checkout ("Pay now") entry point never runs an express
        pre-step (no cart/product bind, no `finalize_express_transaction`
        RPC) - the only thing that stashes the order id is this controller,
        driven straight off the `buckaroo_paypal_order_id` transaction-route
        param the `PaymentForm` patch injects. Chain the controller call into
        `_buckaroo_create_payment` to prove that stash is exactly what it
        needs to pop."""
        paypal = self.env.ref("payment_buckaroo_official.payment_method_paypal")
        self.buckaroo.payment_method_ids = [Command.link(paypal.id)]

        from ..controllers.paypal import PaypalPaymentPortal
        from odoo.addons.website_sale.controllers.payment import PaymentPortal

        session = {}
        pm = MagicMock()
        pm.code = "buckaroo_paypal"

        fake_env = MagicMock()
        fake_env.__getitem__.return_value.sudo.return_value.browse.return_value = pm

        fake_req = MagicMock()
        fake_req.env = fake_env
        fake_req.session = session

        controller = PaypalPaymentPortal()
        with (
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.paypal.request",
                new=fake_req,
            ),
            patch.object(
                PaymentPortal,
                "shop_payment_transaction",
                new=lambda self_, order_id_, access_token, **kw: {"ok": True},
            ),
        ):
            controller.shop_payment_transaction(
                1,
                "tok",
                payment_method_id=5,
                buckaroo_paypal_order_id="PP-CHECKOUT-1",
            )
        self.assertEqual(session.get("buckaroo_paypal_order_id"), "PP-CHECKOUT-1")

        tx = self._create_buckaroo_tx(reference="TX-PP-CHECKOUT", payment_method=paypal)
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()
        with patch(_PAYPAL_PS) as MockPS, patch("odoo.http.request", fake_req):
            MockPS.return_value.create_payment.return_value = mock_builder
            paypal._buckaroo_create_payment(tx, client)

        self.assertNotIn("buckaroo_paypal_order_id", session)
        param_calls = {
            call.args[0]: call.args[1] for call in mock_builder.add_parameter.call_args_list
        }
        self.assertEqual(param_calls.get("payPalOrderId"), "PP-CHECKOUT-1")


@tagged("post_install", "-at_install")
class TestPaypalFeeAmounts(BuckarooOfficialCommon):
    """BTI-2182: every amount handed to the PayPal button must include the
    PayPal fee the transaction route adds, or Buckaroo refuses the pay request
    with "different authorized amount"."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.paypal = cls.env.ref("payment_buckaroo_official.payment_method_paypal")
        cls.buckaroo.payment_method_ids = [Command.link(cls.paypal.id)]
        cls.paypal.buckaroo_official_fee_amount = "1.5%"
        cls.product = cls.env["product.product"].create(
            {
                "name": "PayPal Fee Widget",
                "list_price": 36.0,
                "type": "service",
                "taxes_id": [Command.clear()],
            }
        )
        cls.website = cls.env["website"].get_current_website()

    def _make_order(self):
        order = self.env["sale.order"].create(
            {"partner_id": self.partner.id, "website_id": self.website.id}
        )
        self.env["sale.order.line"].create(
            {
                "order_id": order.id,
                "product_id": self.product.id,
                "product_uom_qty": 1,
                "price_unit": 36.0,
                "tax_ids": [Command.clear()],
            }
        )
        return order

    def _patch_wallet_request(self, order=None):
        fake_req = MagicMock()
        fake_req.env = self.env
        fake_req.cart = order.sudo() if order else self.env["sale.order"]
        fake_req.website = self.website
        return patch(
            "odoo.addons.payment_buckaroo_official.controllers.express_shipping.request",
            fake_req,
        )

    def _wallet_controller(self):
        from ..controllers.express_shipping import BuckarooWalletShippingController

        return BuckarooWalletShippingController()

    def test_wallet_amount_includes_paypal_fee(self):
        order = self._make_order()
        with self._patch_wallet_request(order):
            result = self._wallet_controller().buckaroo_wallet_amount(
                payment_method_id=self.paypal.id
            )
        self.assertEqual(result["amount"], 36.54)
        self.assertEqual(result["minor_amount"], 3654)

    def test_wallet_amount_uses_each_wallets_own_fee(self):
        # Apple Pay and Google Pay pass their own method id, so the sheet shows
        # their fee, not PayPal's.
        applepay = self.env.ref("payment_buckaroo_official.payment_method_applepay")
        self.buckaroo.payment_method_ids = [Command.link(applepay.id)]
        applepay.buckaroo_official_fee_amount = "2.00"
        order = self._make_order()
        with self._patch_wallet_request(order):
            result = self._wallet_controller().buckaroo_wallet_amount(payment_method_id=applepay.id)
        self.assertEqual(result["amount"], 38.0)

    def test_wallet_methods_total_includes_the_wallet_fee(self):
        # The inline Apple Pay sheet seeds its carriers from this route.
        order = self._make_order()
        controller = self._wallet_controller()
        with (
            self._patch_wallet_request(order),
            patch.object(
                type(controller), "_get_delivery_methods_express_checkout", return_value={}
            ),
        ):
            result = controller.buckaroo_wallet_methods(payment_method_id=self.paypal.id)
        self.assertEqual(result["amount"], 36.54)

    def test_wallet_amount_without_method_is_the_cart_total(self):
        order = self._make_order()
        with self._patch_wallet_request(order):
            result = self._wallet_controller().buckaroo_wallet_amount()
        self.assertEqual(result["amount"], 36.0)

    def test_wallet_amount_ignores_unknown_method(self):
        order = self._make_order()
        for method_id in (999999, 2**40, -1, "abc", self.payment_method.id):
            with self.subTest(method_id=method_id), self._patch_wallet_request(order):
                result = self._wallet_controller().buckaroo_wallet_amount(
                    payment_method_id=method_id
                )
                self.assertEqual(result["amount"], 36.0)

    def test_wallet_amount_leaves_cart_untouched(self):
        order = self._make_order()
        with self._patch_wallet_request(order):
            self._wallet_controller().buckaroo_wallet_amount(payment_method_id=self.paypal.id)
        self.assertFalse(order.order_line.filtered("is_buckaroo_surcharge"))
        self.assertEqual(order.amount_total, 36.0)

    def test_wallet_amount_without_cart_is_empty(self):
        with self._patch_wallet_request():
            result = self._wallet_controller().buckaroo_wallet_amount(
                payment_method_id=self.paypal.id
            )
        self.assertEqual(result["amount"], 0.0)

    def test_set_method_total_includes_paypal_fee(self):
        order = self._make_order()
        with self._patch_wallet_request(order):
            result = self._wallet_controller().buckaroo_wallet_set_method(
                dm_id="none", payment_method_id=self.paypal.id
            )
        self.assertEqual(result["amount"], 36.54)

    def test_product_methods_subtotal_includes_paypal_fee(self):
        from odoo.addons.website_sale.tests.common import MockRequest

        # Fixed fee: the website pricelist sets the product price, not the test.
        self.paypal.buckaroo_official_fee_amount = "2.00"
        # The throwaway rating order reads the website off the real request.
        with MockRequest(self.env, website=self.website):
            controller = self._wallet_controller()
            with_fee = controller.buckaroo_wallet_product_methods(
                self.product.id, qty=1, payment_method_id=self.paypal.id
            )
            without_fee = controller.buckaroo_wallet_product_methods(self.product.id, qty=1)
        self.assertAlmostEqual(with_fee["subtotal"] - without_fee["subtotal"], 2.0, places=2)

    def _second_paypal(self, fee):
        """Another `buckaroo_paypal` record (a duplicated method)."""
        other = self.paypal.copy({"buckaroo_official_fee_amount": fee})
        self.buckaroo.payment_method_ids = [Command.link(other.id)]
        return other

    def _checkout_values(self, order, methods):
        from odoo.addons.website_sale.controllers.main import WebsiteSale

        from ..controllers.fee import BuckarooFeeWebsiteSale

        with patch.object(
            WebsiteSale,
            "_get_shop_payment_values",
            return_value={"amount": 36.0, "payment_methods_sudo": methods},
        ):
            return BuckarooFeeWebsiteSale()._get_shop_payment_values(order)

    def test_checkout_values_carry_paypal_amount(self):
        order = self._make_order()
        values = self._checkout_values(order, self.paypal | self.ideal)
        self.assertEqual(values.get("buckaroo_wallet_amounts"), {self.paypal.id: 36.54})
        values = self._checkout_values(order, self.ideal)
        self.assertNotIn("buckaroo_wallet_amounts", values)
        self.assertFalse(order.order_line.filtered("is_buckaroo_surcharge"))

    def test_checkout_values_cover_every_wallet(self):
        # The Apple Pay and Google Pay sheets show the amount the tx charges.
        applepay = self.env.ref("payment_buckaroo_official.payment_method_applepay")
        googlepay = self.env.ref("payment_buckaroo_official.payment_method_googlepay")
        self.buckaroo.payment_method_ids = [Command.link(applepay.id), Command.link(googlepay.id)]
        applepay.buckaroo_official_fee_amount = "2.00"
        googlepay.buckaroo_official_fee_amount = ""
        order = self._make_order()
        values = self._checkout_values(order, self.paypal | applepay | googlepay | self.ideal)
        self.assertEqual(
            values.get("buckaroo_wallet_amounts"),
            {self.paypal.id: 36.54, applepay.id: 38.0, googlepay.id: 36.0},
        )

    def test_checkout_values_skip_a_failing_preview(self):
        # One wallet's broken fee must not break the payment page; that
        # wallet gets no amount, so the page shows it as unavailable.
        applepay = self.env.ref("payment_buckaroo_official.payment_method_applepay")
        self.buckaroo.payment_method_ids = [Command.link(applepay.id)]
        order = self._make_order()
        original = type(order)._buckaroo_amount_to_pay_for_method

        def preview(order_self, payment_method):
            if payment_method == applepay:
                raise ValueError("broken surcharge product")
            return original(order_self, payment_method)

        with (
            patch.object(type(order), "_buckaroo_amount_to_pay_for_method", preview),
            self.assertLogs("odoo.addons.payment_buckaroo_official.controllers.fee", "ERROR"),
        ):
            values = self._checkout_values(order, self.paypal | applepay)
        self.assertEqual(
            values.get("buckaroo_wallet_amounts"), {self.paypal.id: 36.54, applepay.id: None}
        )

    def test_transaction_route_moves_sheet_amount_into_context(self):
        # Core rejects unknown kwargs: the sheet total must not reach it.
        from odoo.addons.website_sale.controllers.payment import PaymentPortal

        from ..controllers import fee as fee_module
        from ..utils import const

        captured = {}

        def fake_super(self_, order_id, access_token, **kwargs):
            captured["kwargs"] = kwargs
            captured["context"] = dict(fee_module.request.env.context)
            return {}

        fake_req = MagicMock()
        fake_req.env = self.env

        def update_context(**ctx):
            fake_req.env = fake_req.env(context={**fake_req.env.context, **ctx})

        fake_req.update_context.side_effect = update_context
        with (
            patch.object(fee_module, "request", fake_req),
            patch.object(PaymentPortal, "shop_payment_transaction", fake_super),
        ):
            fee_module.BuckarooFeePaymentPortal().shop_payment_transaction(
                1, "token", payment_method_id=self.paypal.id, buckaroo_wallet_amount=36.54
            )
        self.assertNotIn("buckaroo_wallet_amount", captured["kwargs"])
        self.assertEqual(captured["context"][const.WALLET_SHEET_AMOUNT_CONTEXT_KEY], 36.54)

    def test_checkout_values_key_each_paypal_method_by_id(self):
        # Each PayPal button must authorize its own method's fee.
        other = self._second_paypal("2.00")
        order = self._make_order()
        values = self._checkout_values(order, self.paypal | other)
        self.assertEqual(
            values.get("buckaroo_wallet_amounts"), {self.paypal.id: 36.54, other.id: 38.0}
        )
