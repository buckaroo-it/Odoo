# Part of Odoo. See LICENSE file for full copyright and licensing details.

import inspect
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import BuckarooOfficialCommon


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

    def _call(self, *, pm_code="buckaroo_paypal", order_id="PP-ORDER-1", super_raises=False):
        from ..controllers.paypal import PaypalPaymentPortal
        from odoo.addons.website_sale.controllers.payment import PaymentPortal

        session = {}
        pm = MagicMock()
        pm.code = pm_code

        fake_env = MagicMock()
        fake_env.__getitem__.return_value.sudo.return_value.browse.return_value = pm

        fake_req = MagicMock()
        fake_req.env = fake_env
        fake_req.session = session

        kwargs = {"payment_method_id": 5}
        if order_id is not None:
            kwargs["buckaroo_paypal_order_id"] = order_id

        def _super(self_, order_id_, access_token, **kw):
            if super_raises:
                raise ValueError("boom")
            return {"ok": True}

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
