# Part of Odoo. See LICENSE file for full copyright and licensing details.

import inspect
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.website_sale.tests.common import WebsiteSaleCommon

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
            # ``Controller.env`` (read by ``_()``) binds its own ``request``.
            patch("odoo.http.routing_map.request", fake_req),
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

    def test_refusal_without_raising_drops_stashed_order_id(self):
        refused = {"state": "error", "state_message": "", "redirect": "/shop/address"}
        result, session = self._call(super_result=refused)
        self.assertEqual(result, refused)
        self.assertNotIn("buckaroo_paypal_order_id", session)

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
        paypal = self._pm("buckaroo_paypal")

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
            paypal._buckaroo_create_payment(tx.with_context(payment_safe_write=True), client)

        self.assertNotIn("buckaroo_paypal_order_id", session)
        param_calls = {
            call.args[0]: call.args[1] for call in mock_builder.add_parameter.call_args_list
        }
        self.assertEqual(param_calls.get("payPalOrderId"), "PP-CHECKOUT-1")


@tagged("post_install", "-at_install")
class TestPaypalExpressAddressStep(WebsiteSaleCommon):
    """Odoo 20 runs the checkout steps on ``/shop/payment/transaction``. A
    PayPal express order only has the "PayPal Customer" stub address until
    capture, so the address step must let it through - and only it."""

    TX_STEP = "/shop/payment/transaction"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.buyer = new_test_user(cls.env, login="pp_express_buyer", groups="base.group_portal")
        # The stub `_ppPartialToAddress` sends: no street, phone or email.
        cls.buyer.partner_id.write(
            {
                "name": "PayPal Customer",
                "country_id": cls.env.ref("base.nl").id,
                "zip": "1012AB",
                "city": "Amsterdam",
                "street": False,
                "phone": False,
                "email": False,
            }
        )
        cls.order = cls._create_so(partner_id=cls.buyer.partner_id.id)
        cls.order._set_delivery_method(cls.carrier)

    def _validate(self, order, express=False):
        ctx = {"website_id": self.website.id}
        if express:
            ctx["buckaroo_paypal_express"] = True
        env = self.env(user=self.buyer, context=dict(self.env.context, **ctx))
        return env["website.checkout.step"].validate_checkout_progress(
            self.TX_STEP, order.with_env(env).sudo()
        )

    def test_stub_address_is_rejected_without_flag(self):
        redirect = self._validate(self.order)
        self.assertTrue(redirect and redirect.startswith("/shop/address"), redirect)

    def test_stub_address_passes_with_express_flag(self):
        self.assertIsNone(self._validate(self.order, express=True))

    def test_anonymous_cart_still_redirects_with_express_flag(self):
        order = self._create_so(partner_id=self.public_partner.id)
        order._set_delivery_method(self.carrier)
        self.assertEqual(self._validate(order, express=True), "/shop/address")

    def test_missing_carrier_still_blocks_with_express_flag(self):
        order = self._create_so(partner_id=self.buyer.partner_id.id)
        self.assertEqual(self._validate(order, express=True), "/shop/checkout")
