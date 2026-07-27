# Part of Odoo. See LICENSE file for full copyright and licensing details.

import base64
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import HttpCase, tagged

from .common import BuckarooOfficialCommon, make_mock_sdk_builder


class _patch_request:
    def __init__(self, session_dict):
        mock_req = MagicMock()
        mock_req.session = session_dict
        self._patch = patch("odoo.http.request", mock_req)

    def __enter__(self):
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


@tagged("post_install", "-at_install")
class TestApplepayPaymentMethodRecord(BuckarooOfficialCommon):
    def test_applepay_record_loads_with_expected_fields(self):
        method = self.env.ref("payment_buckaroo_official.payment_method_applepay")

        self.assertEqual(method.code, "buckaroo_applepay")
        self.assertEqual(method.buckaroo_official_sdk_service_name, "applepay")
        self.assertTrue(method.active)
        self.assertEqual(
            set(method.supported_currency_ids.mapped("name")),
            {"EUR"},
        )


@tagged("post_install", "-at_install")
class TestApplepayConfigFieldDefaults(BuckarooOfficialCommon):
    def _defaults(self):
        return self.env["payment.method"].default_get(
            [
                "buckaroo_official_applepay_show_on_cart",
                "buckaroo_official_applepay_show_on_product",
                "buckaroo_official_applepay_show_on_checkout",
                "buckaroo_official_applepay_button_style",
                "buckaroo_official_applepay_integration_mode",
            ]
        )

    def test_show_on_cart_defaults_true(self):
        self.assertTrue(self._defaults()["buckaroo_official_applepay_show_on_cart"])

    def test_show_on_product_defaults_false(self):
        self.assertFalse(self._defaults().get("buckaroo_official_applepay_show_on_product"))

    def test_show_on_checkout_defaults_true(self):
        self.assertTrue(self._defaults()["buckaroo_official_applepay_show_on_checkout"])

    def test_button_style_defaults_black(self):
        self.assertEqual(
            self._defaults()["buckaroo_official_applepay_button_style"],
            "black",
        )

    def test_integration_mode_defaults_inline(self):
        self.assertEqual(
            self._defaults()["buckaroo_official_applepay_integration_mode"],
            "inline",
        )


@tagged("post_install", "-at_install")
class TestApplepayAdminFields(BuckarooOfficialCommon):
    def test_applepay_merchant_guid_persists(self):
        method = self.env.ref("payment_buckaroo_official.payment_method_applepay")
        method.buckaroo_official_applepay_merchant_guid = "BUCK-APPLE-GUID-123"

        method.invalidate_recordset()
        self.assertEqual(
            method.buckaroo_official_applepay_merchant_guid,
            "BUCK-APPLE-GUID-123",
        )


@tagged("post_install", "-at_install")
class TestApplepayIsConfigured(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.applepay = cls.env.ref("payment_buckaroo_official.payment_method_applepay")

    def test_not_configured_without_merchant_guid(self):
        self.applepay.buckaroo_official_applepay_merchant_guid = ""
        self.assertFalse(self.applepay._buckaroo_applepay_is_configured())

    def test_configured_when_merchant_guid_set(self):
        self.applepay.buckaroo_official_applepay_merchant_guid = "BUCK-APPLE-GUID"
        self.assertTrue(self.applepay._buckaroo_applepay_is_configured())


@tagged("post_install", "-at_install")
class TestApplepayCompatibilityFilter(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.applepay = cls.env.ref("payment_buckaroo_official.payment_method_applepay")
        cls.buckaroo.payment_method_ids = [Command.link(cls.applepay.id)]

    def _compatible_codes(self, is_express_checkout=False):
        methods = self.env["payment.method"]._get_compatible_payment_methods(
            provider_ids=self.buckaroo.ids,
            partner_id=self.env.ref("base.partner_admin").id,
            currency_id=self.env.ref("base.EUR").id,
            is_express_checkout=is_express_checkout,
        )
        return set(methods.mapped("code"))

    def test_hidden_when_merchant_guid_unconfigured(self):
        self.applepay.buckaroo_official_applepay_merchant_guid = ""
        self.assertNotIn("buckaroo_applepay", self._compatible_codes())

    def test_visible_on_checkout_when_configured(self):
        self.applepay.write(
            {
                "buckaroo_official_applepay_merchant_guid": "BUCK-APPLE-GUID",
                "buckaroo_official_applepay_show_on_checkout": True,
            }
        )
        self.assertIn("buckaroo_applepay", self._compatible_codes())

    def test_hidden_on_checkout_when_show_on_checkout_false(self):
        self.applepay.write(
            {
                "buckaroo_official_applepay_merchant_guid": "BUCK-APPLE-GUID",
                "buckaroo_official_applepay_show_on_checkout": False,
            }
        )
        self.assertNotIn("buckaroo_applepay", self._compatible_codes())

    def test_express_filter_keeps_applepay_when_show_on_cart_true(self):
        self.applepay.write(
            {
                "buckaroo_official_applepay_merchant_guid": "BUCK-APPLE-GUID",
                "buckaroo_official_applepay_show_on_cart": True,
            }
        )
        self.assertIn("buckaroo_applepay", self._compatible_codes(is_express_checkout=True))

    def test_express_filter_drops_applepay_when_show_on_cart_false(self):
        self.applepay.write(
            {
                "buckaroo_official_applepay_merchant_guid": "BUCK-APPLE-GUID",
                "buckaroo_official_applepay_show_on_cart": False,
            }
        )
        self.assertNotIn("buckaroo_applepay", self._compatible_codes(is_express_checkout=True))

    def test_show_on_checkout_false_does_not_affect_express_cart_flow(self):
        self.applepay.write(
            {
                "buckaroo_official_applepay_merchant_guid": "BUCK-APPLE-GUID",
                "buckaroo_official_applepay_show_on_checkout": False,
                "buckaroo_official_applepay_show_on_cart": True,
            }
        )
        self.assertIn("buckaroo_applepay", self._compatible_codes(is_express_checkout=True))


@tagged("post_install", "-at_install")
class TestApplepaySessionBridgeController(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.applepay = cls.env.ref("payment_buckaroo_official.payment_method_applepay")

    def _call_controller(self, payment_method_id=None, **kwargs):
        import odoo.http
        from ..controllers.applepay import ApplepayPaymentPortal

        portal = ApplepayPaymentPortal()
        fake_req = MagicMock()
        fake_req.env = self.env
        fake_req.session = {}
        if payment_method_id is not None:
            kwargs["payment_method_id"] = payment_method_id
        with (
            patch(
                "odoo.addons.website_sale.controllers.payment.PaymentPortal.shop_payment_transaction",
                return_value="SUPER_OK",
            ) as super_stub,
            patch.object(
                odoo.http,
                "request",
                fake_req,
            ),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.applepay.request",
                new=fake_req,
            ),
        ):
            result = portal.shop_payment_transaction(
                order_id=1,
                access_token="tok",
                **kwargs,
            )
            return result, super_stub, fake_req

    def test_token_and_customer_name_stashed_on_session_and_stripped_from_kwargs(self):
        result, super_stub, fake_req = self._call_controller(
            payment_method_id=self.applepay.id,
            buckaroo_applepay_token="ap-tok-abc",
            buckaroo_applepay_customer_name="Ada Lovelace",
        )
        self.assertEqual(result, "SUPER_OK")
        self.assertEqual(
            fake_req.session.get("buckaroo_applepay_token"),
            "ap-tok-abc",
        )
        self.assertEqual(
            fake_req.session.get("buckaroo_applepay_customer_name"),
            "Ada Lovelace",
        )
        _, kwargs_to_super = super_stub.call_args
        self.assertNotIn("buckaroo_applepay_token", kwargs_to_super)
        self.assertNotIn("buckaroo_applepay_customer_name", kwargs_to_super)

    def test_no_applepay_kwargs_passes_through_unchanged(self):
        result, super_stub, fake_req = self._call_controller()
        self.assertEqual(result, "SUPER_OK")
        self.assertNotIn("buckaroo_applepay_token", fake_req.session)
        self.assertNotIn("buckaroo_applepay_customer_name", fake_req.session)

    def test_non_applepay_method_does_not_stash_session(self):
        result, super_stub, fake_req = self._call_controller(
            payment_method_id=self.ideal.id,
            buckaroo_applepay_token="leak-attempt",
            buckaroo_applepay_customer_name="Leak",
        )
        self.assertEqual(result, "SUPER_OK")
        self.assertNotIn("buckaroo_applepay_token", fake_req.session)
        self.assertNotIn("buckaroo_applepay_customer_name", fake_req.session)

    def test_empty_token_after_strip_is_not_stashed(self):
        _, _, fake_req = self._call_controller(
            payment_method_id=self.applepay.id,
            buckaroo_applepay_token="   ",
            buckaroo_applepay_customer_name="Ada Lovelace",
        )
        self.assertNotIn("buckaroo_applepay_token", fake_req.session)
        self.assertEqual(
            fake_req.session.get("buckaroo_applepay_customer_name"),
            "Ada Lovelace",
        )

    def test_empty_customer_name_after_strip_is_not_stashed(self):
        _, _, fake_req = self._call_controller(
            payment_method_id=self.applepay.id,
            buckaroo_applepay_token="ap-tok",
            buckaroo_applepay_customer_name="   ",
        )
        self.assertEqual(
            fake_req.session.get("buckaroo_applepay_token"),
            "ap-tok",
        )
        self.assertNotIn("buckaroo_applepay_customer_name", fake_req.session)

    def test_token_is_length_capped_to_8192(self):
        oversized = "a" * 20000
        _, _, fake_req = self._call_controller(
            payment_method_id=self.applepay.id,
            buckaroo_applepay_token=oversized,
            buckaroo_applepay_customer_name="Ada",
        )
        stashed = fake_req.session.get("buckaroo_applepay_token")
        self.assertIsNotNone(stashed)
        self.assertEqual(len(stashed), 8192)

    def test_customer_name_is_length_capped_to_128(self):
        oversized = "A" * 500
        _, _, fake_req = self._call_controller(
            payment_method_id=self.applepay.id,
            buckaroo_applepay_token="ap-tok",
            buckaroo_applepay_customer_name=oversized,
        )
        stashed = fake_req.session.get("buckaroo_applepay_customer_name")
        self.assertIsNotNone(stashed)
        self.assertEqual(len(stashed), 128)

    def test_customer_name_strips_control_characters(self):
        _, _, fake_req = self._call_controller(
            payment_method_id=self.applepay.id,
            buckaroo_applepay_token="ap-tok",
            buckaroo_applepay_customer_name="Ada\x00\x01\x1fLovelace\x7f",
        )
        self.assertEqual(
            fake_req.session.get("buckaroo_applepay_customer_name"),
            "AdaLovelace",
        )

    def test_token_and_customer_name_are_stripped_of_surrounding_whitespace(self):
        _, _, fake_req = self._call_controller(
            payment_method_id=self.applepay.id,
            buckaroo_applepay_token="  ap-tok  ",
            buckaroo_applepay_customer_name="  Ada Lovelace  ",
        )
        self.assertEqual(
            fake_req.session.get("buckaroo_applepay_token"),
            "ap-tok",
        )
        self.assertEqual(
            fake_req.session.get("buckaroo_applepay_customer_name"),
            "Ada Lovelace",
        )


@tagged("post_install", "-at_install")
class TestApplepayCreatePayment(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.applepay = cls.env.ref("payment_buckaroo_official.payment_method_applepay")
        cls.buckaroo.payment_method_ids = [Command.link(cls.applepay.id)]

    def test_create_payment_sends_payment_data_and_customer_name_when_session_has_them(self):
        tx = self._create_buckaroo_tx(
            reference="TX-AP-001",
            payment_method=self.applepay,
        )
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_applepay.PaymentService",
            ) as MockPS,
            _patch_request(
                {
                    "buckaroo_applepay_token": "ap-token-xyz",
                    "buckaroo_applepay_customer_name": "Grace Hopper",
                }
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.applepay._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        self.assertEqual(result, mock_response)

        param_calls = {
            call.args[0]: call.args[1] for call in mock_builder.add_parameter.call_args_list
        }
        expected_payment_data = base64.b64encode(b"ap-token-xyz").decode("ascii")
        self.assertEqual(param_calls.get("PaymentData"), expected_payment_data)
        self.assertEqual(param_calls.get("CustomerCardName"), "Grace Hopper")

        service_arg = MockPS.return_value.create_payment.call_args[0][0]
        self.assertEqual(service_arg, "applepay")

    def test_create_payment_pops_session_keys_after_use(self):
        tx = self._create_buckaroo_tx(
            reference="TX-AP-POP",
            payment_method=self.applepay,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        session_dict = {
            "buckaroo_applepay_token": "ap-pop-token",
            "buckaroo_applepay_customer_name": "Linus Torvalds",
        }
        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_applepay.PaymentService",
            ) as MockPS,
            _patch_request(session_dict),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            self.applepay._buckaroo_create_payment(tx, client)

        self.assertNotIn("buckaroo_applepay_token", session_dict)
        self.assertNotIn("buckaroo_applepay_customer_name", session_dict)

    def test_create_payment_raises_validation_error_when_token_missing(self):
        tx = self._create_buckaroo_tx(
            reference="TX-AP-NO-TOK",
            payment_method=self.applepay,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_applepay.PaymentService",
            ) as MockPS,
            _patch_request(
                {
                    "buckaroo_applepay_customer_name": "Token Missing",
                }
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            with self.assertRaises(ValidationError):
                self.applepay._buckaroo_create_payment(tx, client)

    def test_create_payment_raises_validation_error_when_customer_name_missing(self):
        tx = self._create_buckaroo_tx(
            reference="TX-AP-NO-NAME",
            payment_method=self.applepay,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_applepay.PaymentService",
            ) as MockPS,
            _patch_request(
                {
                    "buckaroo_applepay_token": "ap-token-without-name",
                }
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            with self.assertRaises(ValidationError):
                self.applepay._buckaroo_create_payment(tx, client)

    def test_non_applepay_method_delegates_to_base(self):
        tx = self._create_buckaroo_tx(reference="TX-IDEAL-AP-OFF")
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService",
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        param_keys = [call.args[0] for call in mock_builder.add_parameter.call_args_list]
        self.assertNotIn("PaymentData", param_keys)
        self.assertNotIn("CustomerCardName", param_keys)

    def test_redirect_mode_uses_generic_flow_without_session_token(self):
        # Redirect mode must defer to the base hosted-redirect flow: no Apple
        # Pay token read from the session, no PaymentData/CustomerCardName.
        self.applepay.buckaroo_official_applepay_integration_mode = "redirect"
        tx = self._create_buckaroo_tx(
            reference="TX-AP-REDIRECT",
            payment_method=self.applepay,
        )
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService",
            ) as MockPS,
            _patch_request({}),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.applepay._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        self.assertEqual(result, mock_response)
        param_keys = [call.args[0] for call in mock_builder.add_parameter.call_args_list]
        self.assertNotIn("PaymentData", param_keys)
        self.assertNotIn("CustomerCardName", param_keys)
        service_arg = MockPS.return_value.create_payment.call_args[0][0]
        self.assertEqual(service_arg, "applepay")


@tagged("post_install", "-at_install")
class TestApplepayWellKnownRoute(BuckarooOfficialCommon):
    """The `.well-known` route serves the committed Buckaroo file."""

    @staticmethod
    def _committed_file_body():
        from odoo.tools import file_open

        from ..controllers.applepay import ApplepayWellKnownController

        with file_open(ApplepayWellKnownController._DOMAIN_ASSOCIATION_FILE) as f:
            return f.read()

    def test_serves_committed_file_with_200_text_plain(self):
        from ..controllers.applepay import ApplepayWellKnownController

        resp = ApplepayWellKnownController().apple_pay_get_domain_association_file()

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/plain")
        self.assertEqual(resp.get_data(as_text=True), self._committed_file_body())

    def test_committed_file_is_the_buckaroo_pspid(self):
        # The body is the hex-encoded JSON; the Buckaroo pspId 96DD41F0… is
        # present once decoded. Confirms we ship Buckaroo's file, not a stub.
        body = self._committed_file_body()
        decoded = bytes.fromhex(body).decode("ascii")
        self.assertIn('"pspId":"96DD41F0', decoded)


@tagged("post_install", "-at_install")
class TestApplepayWellKnownRouteHttp(HttpCase):
    """End-to-end: the live `.well-known` route serves Buckaroo's committed
    domain-association file as text/plain.

    Note: payment_stripe registers the same path. This controller does not
    override Stripe's, so when both are installed the file actually served
    depends on addon load order; the merchant domain must be verified against
    whichever file wins in the deployed environment.
    """

    def test_route_serves_buckaroo_domain_association_file(self):
        from odoo.tools import file_open

        from ..controllers.applepay import ApplepayWellKnownController

        with file_open(ApplepayWellKnownController._DOMAIN_ASSOCIATION_FILE) as f:
            expected = f.read()

        resp = self.url_open("/.well-known/apple-developer-merchantid-domain-association")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["Content-Type"].startswith("text/plain"))
        self.assertEqual(resp.text, expected)


@tagged("post_install", "-at_install")
class TestApplepayExpressCheckoutCapability(BuckarooOfficialCommon):
    def test_buckaroo_provider_supports_express_checkout(self):
        self.assertTrue(self.buckaroo.support_express_checkout)

    def test_applepay_method_supports_express_checkout(self):
        method = self.env.ref("payment_buckaroo_official.payment_method_applepay")
        self.assertTrue(method.support_express_checkout)

    def test_express_checkout_form_view_renders_applepay(self):
        # The provider's single express form view is the wrapper, which must
        # pull in the Apple Pay express button alongside Google Pay's.
        view = self.buckaroo.express_checkout_form_view_id
        self.assertEqual(
            view.xml_id,
            "payment_buckaroo_official.buckaroo_express_checkout_form",
        )
        self.assertIn(
            "payment_buckaroo_official.applepay_express_form",
            view.arch,
        )


@tagged("post_install", "-at_install")
class TestApplepayExpressFormIntegrationModeGate(BuckarooOfficialCommon):
    """The express button must not render in redirect mode.

    In redirect mode the native sheet would authorize a token that the
    hosted-redirect flow discards, so the cart express button is gated on
    ``integration_mode == 'inline'`` and the method falls through to the
    plain hosted flow.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.applepay = cls.env.ref("payment_buckaroo_official.payment_method_applepay")
        cls.buckaroo.payment_method_ids = [Command.link(cls.applepay.id)]
        cls.applepay.write(
            {
                "buckaroo_official_applepay_merchant_guid": "BUCK-APPLE-GUID",
                "buckaroo_official_applepay_show_on_cart": True,
            }
        )

    def _render_express_form(self):
        view = self.env.ref("payment_buckaroo_official.applepay_express_form")
        # The template only dereferences `request.env`; supply a stand-in
        # request and use `minimal_qcontext` so QWeb does not overwrite it
        # with the (unbound, in tests) global request proxy.
        fake_req = MagicMock()
        fake_req.env = self.env
        qweb = self.env["ir.qweb"].with_context(minimal_qcontext=True)
        return str(
            qweb._render(
                view.id,
                {
                    "provider_sudo": self.buckaroo,
                    "partner_id": self.partner.id,
                    "request": fake_req,
                },
            )
        )

    def test_inline_mode_renders_express_button(self):
        self.applepay.buckaroo_official_applepay_integration_mode = "inline"
        self.assertIn(
            "o_buckaroo_applepay_express_container",
            self._render_express_form(),
        )

    def test_redirect_mode_omits_express_button(self):
        self.applepay.buckaroo_official_applepay_integration_mode = "redirect"
        self.assertNotIn(
            "o_buckaroo_applepay_express_container",
            self._render_express_form(),
        )


@tagged("post_install", "-at_install")
class TestApplepayProductExpressInitController(BuckarooOfficialCommon):
    """Unit-tests for `/shop/buckaroo/applepay/express_init`.

    The endpoint adds a product to the cart and returns the same payload
    `WebsiteSale._get_express_shop_payment_values` returns for the cart
    page, so the express flow can run from the product page.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.applepay = cls.env.ref("payment_buckaroo_official.payment_method_applepay")

    def _call_endpoint(
        self,
        product_id=1,
        qty=1,
        no_variant_attribute_value_ids=None,
        product_custom_attribute_values=None,
        product_exists=True,
        is_public_user=False,
        express_payload=None,
    ):
        """Invoke the controller with a mocked `request`.

        Returns ``(result, add_to_cart_mock, mock_product)``.
        """
        from ..controllers.express_applepay import (
            BuckarooApplepayExpressController,
        )

        mock_cart = MagicMock()
        # Currency record on the live cart, used by the endpoint to fill
        # `currency_code` in the response payload.
        mock_cart.currency_id.name = "EUR"

        mock_product = MagicMock()
        mock_product.product_tmpl_id.id = 99
        if product_exists:
            mock_product.exists.return_value = mock_product
        else:
            # Falsy stand-in for an empty `product.product` recordset.
            mock_product.exists.return_value = []

        fake_env = MagicMock()
        fake_env.__getitem__.return_value.browse.return_value = mock_product
        # Translation lookup walks `request.env.lang` for `_()` resolution.
        fake_env.lang = "en_US"

        fake_website = MagicMock()
        fake_website.is_public_user.return_value = is_public_user

        fake_req = MagicMock()
        fake_req.cart = mock_cart
        fake_req.env = fake_env
        fake_req.website = fake_website

        controller = BuckarooApplepayExpressController()
        payload = (
            express_payload
            if express_payload is not None
            else {
                "amount": 12.34,
                "minor_amount": 1234,
                "currency": mock_cart.currency_id,
                "partner_id": 7,
                "transaction_route": "/shop/payment/transaction/99",
                "express_checkout_route": "/shop/express_checkout",
                "shipping_info_required": False,
            }
        )
        import odoo.http

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.express_wallet.request",
                new=fake_req,
            ),
            patch.object(
                odoo.http,
                "request",
                fake_req,
            ),
            patch.object(
                BuckarooApplepayExpressController,
                "add_to_cart",
                return_value={"cart_quantity": qty or 0},
            ) as add_to_cart_mock,
            patch.object(
                BuckarooApplepayExpressController,
                "_get_express_shop_payment_values",
                return_value=dict(payload),
            ),
        ):
            kwargs = {"product_id": product_id, "qty": qty}
            if no_variant_attribute_value_ids is not None:
                kwargs["no_variant_attribute_value_ids"] = no_variant_attribute_value_ids
            if product_custom_attribute_values is not None:
                kwargs["product_custom_attribute_values"] = product_custom_attribute_values
            try:
                result = controller.applepay_express_init(**kwargs)
            except Exception as err:
                return err, add_to_cart_mock, mock_product
            return result, add_to_cart_mock, mock_product

    def test_missing_product_id_raises_user_error(self):
        from odoo.exceptions import UserError

        result, *_ = self._call_endpoint(product_id=None)
        self.assertIsInstance(result, UserError)

    def test_missing_qty_raises_user_error(self):
        from odoo.exceptions import UserError

        result, *_ = self._call_endpoint(product_id=10, qty=None)
        self.assertIsInstance(result, UserError)

    def test_zero_qty_raises_user_error(self):
        from odoo.exceptions import UserError

        result, *_ = self._call_endpoint(product_id=10, qty=0)
        self.assertIsInstance(result, UserError)

    def test_negative_qty_raises_user_error(self):
        from odoo.exceptions import UserError

        result, *_ = self._call_endpoint(product_id=10, qty=-3)
        self.assertIsInstance(result, UserError)

    def test_non_existent_product_raises_user_error(self):
        from odoo.exceptions import UserError

        result, *_ = self._call_endpoint(product_id=99999, product_exists=False)
        self.assertIsInstance(result, UserError)

    def test_valid_call_delegates_to_add_to_cart_with_product_id_and_qty(self):
        result, add_to_cart_mock, _ = self._call_endpoint(
            product_id=15,
            qty=2,
        )
        self.assertNotIsInstance(result, Exception)
        add_to_cart_mock.assert_called_once()
        kwargs = add_to_cart_mock.call_args.kwargs
        self.assertEqual(kwargs.get("product_id"), 15)
        self.assertEqual(kwargs.get("quantity"), 2)
        self.assertEqual(kwargs.get("product_template_id"), 99)

    def test_valid_call_returns_express_payment_values_with_currency_code(self):
        result, *_ = self._call_endpoint(product_id=15, qty=1)
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("amount"), 12.34)
        self.assertEqual(result.get("minor_amount"), 1234)
        # `_get_express_shop_payment_values` returns a `currency` recordset;
        # the endpoint surfaces the ISO code as `currency_code` for the JS.
        self.assertEqual(result.get("currency_code"), "EUR")

    def test_no_variant_attribute_value_ids_propagated_to_add_to_cart(self):
        result, add_to_cart_mock, _ = self._call_endpoint(
            product_id=15,
            qty=1,
            no_variant_attribute_value_ids=[7, 9],
        )
        self.assertNotIsInstance(result, Exception)
        kwargs = add_to_cart_mock.call_args.kwargs
        self.assertEqual(
            kwargs.get("no_variant_attribute_value_ids"),
            [7, 9],
        )

    def test_product_custom_attribute_values_propagated_to_add_to_cart(self):
        custom = [
            {
                "custom_product_template_attribute_value_id": 4,
                "custom_value": "Engraving: A",
            }
        ]
        result, add_to_cart_mock, _ = self._call_endpoint(
            product_id=15,
            qty=1,
            product_custom_attribute_values=custom,
        )
        self.assertNotIsInstance(result, Exception)
        kwargs = add_to_cart_mock.call_args.kwargs
        self.assertEqual(
            kwargs.get("product_custom_attribute_values"),
            custom,
        )

    def test_combination_id_not_in_signature(self):
        # combination_id is not consumed by `_cart_add` and not part of the
        # controller signature. Calling the bare method with it must raise
        # TypeError; in production, Odoo's HTTP dispatcher silently drops
        # args that aren't in the signature.
        from ..controllers.express_applepay import (
            BuckarooApplepayExpressController,
        )
        import inspect

        sig = inspect.signature(BuckarooApplepayExpressController.applepay_express_init)
        self.assertNotIn("combination_id", sig.parameters)
        self.assertNotIn("product_template_id", sig.parameters)
        self.assertNotIn("kwargs", sig.parameters)

    def test_real_cart_add_charges_chosen_variant(self):
        # Integration test: hit the real `_cart_add` (no mock cart) with a
        # variant product and assert the dedicated express order carries the
        # variant we asked for - while the shopper's existing cart is left
        # untouched. Catches regressions where the endpoint ignores variant
        # params, or where the buy-now leaks into (or charges) the real cart.
        attribute = self.env["product.attribute"].create(
            {
                "name": "AP Test Color",
                "create_variant": "always",
                "value_ids": [
                    Command.create({"name": "AP Red"}),
                    Command.create({"name": "AP Blue"}),
                ],
            }
        )
        template = self.env["product.template"].create(
            {
                "name": "AP Test Variant Product",
                "list_price": 12.50,
                "website_published": True,
                "sale_ok": True,
                "attribute_line_ids": [
                    Command.create(
                        {
                            "attribute_id": attribute.id,
                            "value_ids": [Command.set(attribute.value_ids.ids)],
                        }
                    )
                ],
            }
        )
        # Pick the "AP Blue" variant to ensure we're not just adding the
        # first/default variant.
        blue_variant = template.product_variant_ids.filtered(
            lambda v: any("AP Blue" in val.name for val in v.product_template_variant_value_ids)
        )
        self.assertTrue(blue_variant, "Expected an AP Blue variant.")

        website = self.env["website"].search([], limit=1)
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "website_id": website.id,
                "company_id": website.company_id.id,
            }
        )

        from ..controllers.express_applepay import (
            BuckarooApplepayExpressController,
        )

        controller = BuckarooApplepayExpressController()

        fake_req = MagicMock()
        # The shopper's pre-existing cart, which the buy-now must not touch.
        fake_req.cart = order
        fake_req.env = self.env
        fake_req.website = website
        # `_prepare_sale_order_values` (used to build the dedicated express
        # order) reads these off the request.
        fake_req.fiscal_position = self.env["account.fiscal.position"]
        fake_req.pricelist = self.env["product.pricelist"].search([], limit=1)

        import odoo.http

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.express_wallet.request",
                new=fake_req,
            ),
            patch(
                "odoo.addons.website_sale.controllers.cart.request",
                new=fake_req,
            ),
            patch(
                "odoo.addons.website_sale.models.website.request",
                new=fake_req,
            ),
            patch.object(
                odoo.http,
                "request",
                fake_req,
            ),
            patch.object(
                BuckarooApplepayExpressController,
                "_get_express_shop_payment_values",
                side_effect=lambda o, **kw: {"amount": o.amount_total},
            ),
        ):
            result = controller.applepay_express_init(
                product_id=blue_variant.id,
                qty=3,
            )

        self.assertIsInstance(result, dict)
        # The buy-now built its own order (now `request.cart`); the variant and
        # quantity land there, not on the shopper's pre-existing cart.
        express_order = fake_req.cart
        self.assertNotEqual(express_order, order)
        self.assertFalse(order.order_line, "Existing cart must stay untouched.")
        self.assertEqual(express_order.order_line.product_id, blue_variant)
        self.assertEqual(express_order.order_line.product_uom_qty, 3)

    def test_anonymous_user_returns_payload_with_public_partner_sentinel(self):
        result, add_to_cart_mock, _ = self._call_endpoint(
            product_id=15,
            qty=1,
            is_public_user=True,
            express_payload={
                "amount": 5.0,
                "minor_amount": 500,
                "partner_id": -1,  # public user sentinel from upstream
                "transaction_route": "/shop/payment/transaction/1",
                "express_checkout_route": "/shop/express_checkout",
                "shipping_info_required": False,
            },
        )
        self.assertIsInstance(result, dict)
        self.assertEqual(result["partner_id"], -1)
        add_to_cart_mock.assert_called_once()


@tagged("post_install", "-at_install")
class TestApplepayProductExpressIsolation(HttpCase):
    """End-to-end: the product-page express button is an isolated buy-now.

    Regression for the Buckaroo reject "amount (in cents) in the ApplePay data
    ... is not equal to the amount ... in the request object". The native sheet
    authorizes the price of the single shown product, but the transaction charges
    `order.amount_total`. If `express_init` appended to the shopper's session
    cart, that total would include the rest of the cart (and grow on every
    click), so the authorized token never matched and Buckaroo refused. Each call
    must instead spin up its own order holding only the chosen product, leaving
    the cart untouched.
    """

    def setUp(self):
        super().setUp()
        self.website = self.env["website"].search([], limit=1)
        self.env["res.users"].create(
            {
                "name": "AP Express Shopper",
                "login": "ap_express_shopper",
                "password": "ap_express_shopper",
                "group_ids": [Command.set([self.env.ref("base.group_portal").id])],
            }
        )
        common = {
            "type": "service",  # no delivery: keeps the order to a single line
            "sale_ok": True,
            "website_published": True,
        }
        self.product = self.env["product.product"].create(
            {"name": "AP Express Probe", "list_price": 18.0, **common}
        )
        self.filler = self.env["product.product"].create(
            {"name": "AP Cart Filler", "list_price": 40.0, **common}
        )

    def _rpc(self, route, params):
        resp = self.url_open(
            route,
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": params}),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json().get("result")

    def test_express_init_isolated_and_non_accumulating(self):
        self.authenticate("ap_express_shopper", "ap_express_shopper")
        # Seed a real shopping cart with a different product.
        self._rpc(
            "/shop/cart/add",
            {
                "product_template_id": self.filler.product_tmpl_id.id,
                "product_id": self.filler.id,
                # qty 2 (!= the express order's qty 1) so a clobbered cart-badge
                # count would be caught, not masked by a coincidental match.
                "quantity": 2,
            },
        )
        qty_before = self._rpc("/shop/cart/quantity", {})

        order_ids = set()
        for _ in range(3):
            res = self._rpc(
                "/shop/buckaroo/applepay/express_init",
                {"product_id": self.product.id, "qty": 1},
            )
            order_id = int(res["transaction_route"].rsplit("/", 1)[1])
            express = self.env["sale.order"].browse(order_id)
            # Only the shown product, qty 1 - never the filler, never an
            # accumulated quantity.
            self.assertEqual(express.order_line.product_id, self.product)
            self.assertEqual(express.order_line.product_uom_qty, 1)
            order_ids.add(order_id)

        # A fresh order per click - nothing piles onto one growing order.
        self.assertEqual(len(order_ids), 3)
        # The shopper's cart is exactly as it was before the buy-now.
        self.assertEqual(self._rpc("/shop/cart/quantity", {}), qty_before)

    def test_express_checkout_applies_address_to_isolated_order_not_cart(self):
        # The address step must land on the dedicated express order, never on
        # the shopper's real cart.
        self.authenticate("ap_express_shopper", "ap_express_shopper")
        self._rpc(
            "/shop/cart/add",
            {
                "product_template_id": self.filler.product_tmpl_id.id,
                "product_id": self.filler.id,
                # qty 2 (!= the express order's qty 1) so a clobbered cart-badge
                # count would be caught, not masked by a coincidental match.
                "quantity": 2,
            },
        )
        qty_before = self._rpc("/shop/cart/quantity", {})
        res = self._rpc(
            "/shop/buckaroo/applepay/express_init",
            {"product_id": self.product.id, "qty": 1},
        )
        express = self.env["sale.order"].browse(int(res["transaction_route"].rsplit("/", 1)[1]))
        partner_id = self._rpc(
            "/shop/buckaroo/wallet/express_checkout",
            {
                "billing_address": {
                    "name": "Iso Buyer",
                    "street": "Teststraat 1",
                    "city": "Rotterdam",
                    "zip": "3011AA",
                    "country": "NL",
                    "email": "iso@example.com",
                    "phone": "+31600000000",
                }
            },
        )
        self.assertTrue(partner_id, "express_checkout should return the applied partner id")
        # Address recorded on the express order; cart still holds only the filler.
        self.assertEqual(express.partner_invoice_id.city, "Rotterdam")
        self.assertEqual(express.order_line.product_id, self.product)
        self.assertEqual(self._rpc("/shop/cart/quantity", {}), qty_before)

    def test_express_checkout_pins_total_against_address_tax_change(self):
        # The wallet sheet authorizes a total before the address is known.
        # Applying an address in another tax jurisdiction must not move the
        # fiscal position or the charged total, or Buckaroo rejects the mismatch.
        self.authenticate("ap_express_shopper", "ap_express_shopper")
        res = self._rpc(
            "/shop/buckaroo/applepay/express_init",
            {"product_id": self.product.id, "qty": 1},
        )
        express = self.env["sale.order"].browse(int(res["transaction_route"].rsplit("/", 1)[1]))
        fp_before, total_before = express.fiscal_position_id, express.amount_total
        # Precondition: the JP address must map to a DIFFERENT fiscal position,
        # otherwise the assertions below would pass vacuously (no flip to pin).
        jp_partner = self.env["res.partner"].create(
            {"name": "jp probe", "country_id": self.env.ref("base.jp").id}
        )
        self.assertNotEqual(
            self.env["account.fiscal.position"]._get_fiscal_position(jp_partner),
            fp_before,
            "test setup: JP address must flip the fiscal position to be meaningful",
        )
        # Without the pin `_update_address` would drop the total (e.g. 20.70 ->
        # 18.00 when JP maps to a tax-exempt fpos) - the divergence Buckaroo rejects.
        self._rpc(
            "/shop/buckaroo/wallet/express_checkout",
            {
                "billing_address": {
                    "name": "JP Buyer",
                    "street": "1 Chome",
                    "city": "Tokyo",
                    "zip": "1000001",
                    "country": "JP",
                    "email": "jp@example.com",
                    "phone": "+81312345678",
                }
            },
        )
        express.invalidate_recordset()
        self.assertEqual(express.fiscal_position_id, fp_before)
        self.assertEqual(express.amount_total, total_before)

    def test_express_checkout_without_session_order_is_rejected(self):
        # No prior express_init => no express order in session. The route must
        # reject rather than record the address on the shopper's real cart.
        self.authenticate("ap_express_shopper", "ap_express_shopper")
        self._rpc(
            "/shop/cart/add",
            {
                "product_template_id": self.filler.product_tmpl_id.id,
                "product_id": self.filler.id,
                # qty 2 (!= the express order's qty 1) so a clobbered cart-badge
                # count would be caught, not masked by a coincidental match.
                "quantity": 2,
            },
        )
        qty_before = self._rpc("/shop/cart/quantity", {})
        resp = self.url_open(
            "/shop/buckaroo/wallet/express_checkout",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "call",
                    "params": {
                        "billing_address": {
                            "name": "X",
                            "street": "Y 1",
                            "city": "Rotterdam",
                            "zip": "3011AA",
                            "country": "NL",
                            "email": "x@example.com",
                            "phone": "+31600000000",
                        }
                    },
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        payload = resp.json()
        self.assertEqual(
            payload.get("error", {}).get("data", {}).get("name"),
            "werkzeug.exceptions.NotFound",
        )
        # The shopper's cart was not touched by the rejected call.
        self.assertEqual(self._rpc("/shop/cart/quantity", {}), qty_before)
