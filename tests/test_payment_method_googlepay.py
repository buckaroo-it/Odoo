# Part of Odoo. See LICENSE file for full copyright and licensing details.

import base64
from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import tagged

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
class TestGooglepayPaymentMethodRecord(BuckarooOfficialCommon):
    def test_googlepay_record_loads_with_expected_fields(self):
        method = self.env.ref("payment_buckaroo_official.payment_method_googlepay")

        self.assertEqual(method.code, "googlepay")
        self.assertEqual(method.buckaroo_official_sdk_service_name, "googlepay")
        self.assertEqual(method.support_refund, "partial")
        self.assertTrue(method.active)
        self.assertEqual(
            set(method.supported_currency_ids.mapped("name")),
            {"EUR"},
        )


@tagged("post_install", "-at_install")
class TestGooglepayPlacementFieldDefaults(BuckarooOfficialCommon):
    def _defaults(self):
        return self.env["payment.method"].default_get(
            [
                "buckaroo_official_googlepay_show_on_cart",
                "buckaroo_official_googlepay_show_on_product",
                "buckaroo_official_googlepay_show_on_checkout",
                "buckaroo_official_googlepay_button_style",
            ]
        )

    def test_show_on_cart_defaults_true(self):
        self.assertTrue(self._defaults()["buckaroo_official_googlepay_show_on_cart"])

    def test_show_on_product_defaults_false(self):
        self.assertFalse(self._defaults().get("buckaroo_official_googlepay_show_on_product"))

    def test_show_on_checkout_defaults_true(self):
        self.assertTrue(self._defaults()["buckaroo_official_googlepay_show_on_checkout"])

    def test_button_style_defaults_black(self):
        self.assertEqual(
            self._defaults()["buckaroo_official_googlepay_button_style"],
            "black",
        )


@tagged("post_install", "-at_install")
class TestGooglepayAdminFields(BuckarooOfficialCommon):
    def test_googlepay_admin_fields_persist(self):
        method = self.env.ref("payment_buckaroo_official.payment_method_googlepay")
        method.write(
            {
                "buckaroo_official_googlepay_merchant_guid": "BUCK-MERCHANT-GUID-123",
                "buckaroo_official_googlepay_google_merchant_id": "GOOG-MERCHANT-456",
            }
        )

        method.invalidate_recordset()
        self.assertEqual(
            method.buckaroo_official_googlepay_merchant_guid,
            "BUCK-MERCHANT-GUID-123",
        )
        self.assertEqual(
            method.buckaroo_official_googlepay_google_merchant_id,
            "GOOG-MERCHANT-456",
        )


@tagged("post_install", "-at_install")
class TestGooglepaySessionBridgeController(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.googlepay = cls.env.ref("payment_buckaroo_official.payment_method_googlepay")

    def _call_controller(self, payment_method_id=None, **kwargs):
        import odoo.http
        from ..controllers.googlepay import GooglepayPaymentPortal

        portal = GooglepayPaymentPortal()
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
                "odoo.addons.payment_buckaroo_official.controllers.googlepay.request",
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
            payment_method_id=self.googlepay.id,
            buckaroo_googlepay_token="gp-tok-abc",
            buckaroo_googlepay_customer_name="Ada Lovelace",
        )
        self.assertEqual(result, "SUPER_OK")
        self.assertEqual(
            fake_req.session.get("buckaroo_googlepay_token"),
            "gp-tok-abc",
        )
        self.assertEqual(
            fake_req.session.get("buckaroo_googlepay_customer_name"),
            "Ada Lovelace",
        )
        _, kwargs_to_super = super_stub.call_args
        self.assertNotIn("buckaroo_googlepay_token", kwargs_to_super)
        self.assertNotIn("buckaroo_googlepay_customer_name", kwargs_to_super)

    def test_no_googlepay_kwargs_passes_through_unchanged(self):
        result, super_stub, fake_req = self._call_controller()
        self.assertEqual(result, "SUPER_OK")
        self.assertNotIn("buckaroo_googlepay_token", fake_req.session)
        self.assertNotIn("buckaroo_googlepay_customer_name", fake_req.session)

    def test_non_googlepay_method_does_not_stash_session(self):
        result, super_stub, fake_req = self._call_controller(
            payment_method_id=self.ideal.id,
            buckaroo_googlepay_token="leak-attempt",
            buckaroo_googlepay_customer_name="Leak",
        )
        self.assertEqual(result, "SUPER_OK")
        self.assertNotIn("buckaroo_googlepay_token", fake_req.session)
        self.assertNotIn("buckaroo_googlepay_customer_name", fake_req.session)

    def test_empty_token_after_strip_is_not_stashed(self):
        _, _, fake_req = self._call_controller(
            payment_method_id=self.googlepay.id,
            buckaroo_googlepay_token="   ",
            buckaroo_googlepay_customer_name="Ada Lovelace",
        )
        self.assertNotIn("buckaroo_googlepay_token", fake_req.session)
        self.assertEqual(
            fake_req.session.get("buckaroo_googlepay_customer_name"),
            "Ada Lovelace",
        )

    def test_empty_customer_name_after_strip_is_not_stashed(self):
        _, _, fake_req = self._call_controller(
            payment_method_id=self.googlepay.id,
            buckaroo_googlepay_token="gp-tok",
            buckaroo_googlepay_customer_name="   ",
        )
        self.assertEqual(
            fake_req.session.get("buckaroo_googlepay_token"),
            "gp-tok",
        )
        self.assertNotIn("buckaroo_googlepay_customer_name", fake_req.session)

    def test_token_is_length_capped_to_8192(self):
        oversized = "a" * 20000
        _, _, fake_req = self._call_controller(
            payment_method_id=self.googlepay.id,
            buckaroo_googlepay_token=oversized,
            buckaroo_googlepay_customer_name="Ada",
        )
        stashed = fake_req.session.get("buckaroo_googlepay_token")
        self.assertIsNotNone(stashed)
        self.assertEqual(len(stashed), 8192)

    def test_customer_name_is_length_capped_to_128(self):
        oversized = "A" * 500
        _, _, fake_req = self._call_controller(
            payment_method_id=self.googlepay.id,
            buckaroo_googlepay_token="gp-tok",
            buckaroo_googlepay_customer_name=oversized,
        )
        stashed = fake_req.session.get("buckaroo_googlepay_customer_name")
        self.assertIsNotNone(stashed)
        self.assertEqual(len(stashed), 128)

    def test_customer_name_strips_control_characters(self):
        _, _, fake_req = self._call_controller(
            payment_method_id=self.googlepay.id,
            buckaroo_googlepay_token="gp-tok",
            buckaroo_googlepay_customer_name="Ada\x00\x01\x1fLovelace\x7f",
        )
        self.assertEqual(
            fake_req.session.get("buckaroo_googlepay_customer_name"),
            "AdaLovelace",
        )

    def test_token_and_customer_name_are_stripped_of_surrounding_whitespace(self):
        _, _, fake_req = self._call_controller(
            payment_method_id=self.googlepay.id,
            buckaroo_googlepay_token="  gp-tok  ",
            buckaroo_googlepay_customer_name="  Ada Lovelace  ",
        )
        self.assertEqual(
            fake_req.session.get("buckaroo_googlepay_token"),
            "gp-tok",
        )
        self.assertEqual(
            fake_req.session.get("buckaroo_googlepay_customer_name"),
            "Ada Lovelace",
        )

    def test_non_string_values_are_coerced(self):
        _, _, fake_req = self._call_controller(
            payment_method_id=self.googlepay.id,
            buckaroo_googlepay_token=12345,
            buckaroo_googlepay_customer_name=67890,
        )
        self.assertEqual(
            fake_req.session.get("buckaroo_googlepay_token"),
            "12345",
        )
        self.assertEqual(
            fake_req.session.get("buckaroo_googlepay_customer_name"),
            "67890",
        )


@tagged("post_install", "-at_install")
class TestGooglepayCreatePayment(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.googlepay = cls.env.ref("payment_buckaroo_official.payment_method_googlepay")
        cls.buckaroo.payment_method_ids = [Command.link(cls.googlepay.id)]

    def test_create_payment_sends_payment_data_and_customer_name_when_session_has_them(self):
        tx = self._create_buckaroo_tx(
            reference="TX-GP-001",
            payment_method=self.googlepay,
        )
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_googlepay.PaymentService",
            ) as MockPS,
            _patch_request(
                {
                    "buckaroo_googlepay_token": "gp-token-xyz",
                    "buckaroo_googlepay_customer_name": "Grace Hopper",
                }
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.googlepay._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        self.assertEqual(result, mock_response)

        param_calls = {
            call.args[0]: call.args[1] for call in mock_builder.add_parameter.call_args_list
        }
        expected_payment_data = base64.b64encode(b"gp-token-xyz").decode("ascii")
        self.assertEqual(param_calls.get("PaymentData"), expected_payment_data)
        self.assertEqual(param_calls.get("CustomerCardName"), "Grace Hopper")

        service_arg = MockPS.return_value.create_payment.call_args[0][0]
        self.assertEqual(service_arg, "googlepay")

    def test_create_payment_pops_session_keys_after_use(self):
        tx = self._create_buckaroo_tx(
            reference="TX-GP-POP",
            payment_method=self.googlepay,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        session_dict = {
            "buckaroo_googlepay_token": "gp-pop-token",
            "buckaroo_googlepay_customer_name": "Linus Torvalds",
        }
        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_googlepay.PaymentService",
            ) as MockPS,
            _patch_request(session_dict),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            self.googlepay._buckaroo_create_payment(tx, client)

        self.assertNotIn("buckaroo_googlepay_token", session_dict)
        self.assertNotIn("buckaroo_googlepay_customer_name", session_dict)

    def test_create_payment_raises_validation_error_when_token_missing(self):
        tx = self._create_buckaroo_tx(
            reference="TX-GP-NO-TOK",
            payment_method=self.googlepay,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_googlepay.PaymentService",
            ) as MockPS,
            _patch_request(
                {
                    "buckaroo_googlepay_customer_name": "Token Missing",
                }
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            with self.assertRaises(ValidationError):
                self.googlepay._buckaroo_create_payment(tx, client)

    def test_create_payment_raises_validation_error_when_customer_name_missing(self):
        tx = self._create_buckaroo_tx(
            reference="TX-GP-NO-NAME",
            payment_method=self.googlepay,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_googlepay.PaymentService",
            ) as MockPS,
            _patch_request(
                {
                    "buckaroo_googlepay_token": "gp-token-without-name",
                }
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            with self.assertRaises(ValidationError):
                self.googlepay._buckaroo_create_payment(tx, client)

    def test_non_googlepay_method_delegates_to_base(self):
        tx = self._create_buckaroo_tx(reference="TX-IDEAL-GP-OFF")
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


@tagged("post_install", "-at_install")
class TestGooglepayRefundDelegatesToBase(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.googlepay = cls.env.ref("payment_buckaroo_official.payment_method_googlepay")
        cls.buckaroo.payment_method_ids = [Command.link(cls.googlepay.id)]

    def test_refund_delegates_to_base_without_googlepay_params(self):
        source_tx = self._create_buckaroo_tx(
            reference="SRC-GP-REF",
            amount=50.0,
            payment_method=self.googlepay,
        )
        source_tx.provider_reference = "SRC_GP_KEY"
        refund_tx = self._create_buckaroo_tx(
            reference="REF-GP-001",
            amount=-50.0,
            payment_method=self.googlepay,
        )
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService",
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.googlepay._buckaroo_create_refund(
                source_tx,
                refund_tx,
                client,
            )

        mock_builder.refund.assert_called_once()
        self.assertEqual(result, mock_response)

        params = MockPS.return_value.create_payment.call_args[0][1]
        self.assertNotIn("paymentData", params)
        self.assertNotIn("PaymentData", params)
        self.assertNotIn("customerCardName", params)
        self.assertNotIn("CustomerCardName", params)

        param_keys = [call.args[0] for call in mock_builder.add_parameter.call_args_list]
        self.assertNotIn("PaymentData", param_keys)
        self.assertNotIn("CustomerCardName", param_keys)

        service_arg = MockPS.return_value.create_payment.call_args[0][0]
        self.assertEqual(service_arg, "googlepay")


@tagged("post_install", "-at_install")
class TestGooglepayCompatibilityFilter(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.googlepay = cls.env.ref("payment_buckaroo_official.payment_method_googlepay")

    def _compatible_codes(self):
        methods = self.env["payment.method"]._get_compatible_payment_methods(
            provider_ids=self.buckaroo.ids,
            partner_id=self.env.ref("base.partner_admin").id,
            currency_id=self.env.ref("base.EUR").id,
        )
        return set(methods.mapped("code"))

    def test_googlepay_hidden_when_merchant_guid_unconfigured(self):
        self.googlepay.buckaroo_official_googlepay_merchant_guid = ""
        self.assertNotIn("googlepay", self._compatible_codes())

    def test_googlepay_visible_when_merchant_guid_set(self):
        self.googlepay.buckaroo_official_googlepay_merchant_guid = "BUCK-GUID-AAA"
        self.assertIn("googlepay", self._compatible_codes())

    def test_googlepay_hidden_in_prod_when_google_merchant_id_unset(self):
        self.buckaroo.state = "enabled"
        self.googlepay.write(
            {
                "buckaroo_official_googlepay_merchant_guid": "BUCK-GUID-AAA",
                "buckaroo_official_googlepay_google_merchant_id": "",
            }
        )
        self.assertNotIn("googlepay", self._compatible_codes())

    def test_googlepay_visible_in_prod_when_fully_configured(self):
        self.buckaroo.state = "enabled"
        self.googlepay.write(
            {
                "buckaroo_official_googlepay_merchant_guid": "BUCK-GUID-AAA",
                "buckaroo_official_googlepay_google_merchant_id": "GOOG-MID-BBB",
            }
        )
        self.assertIn("googlepay", self._compatible_codes())

    def test_googlepay_visible_in_test_without_google_merchant_id(self):
        self.buckaroo.state = "test"
        self.googlepay.write(
            {
                "buckaroo_official_googlepay_merchant_guid": "BUCK-GUID-AAA",
                "buckaroo_official_googlepay_google_merchant_id": "",
            }
        )
        self.assertIn("googlepay", self._compatible_codes())


@tagged("post_install", "-at_install")
class TestGooglepayExpressCheckoutCapability(BuckarooOfficialCommon):
    def test_buckaroo_provider_supports_express_checkout(self):
        self.assertTrue(self.buckaroo.support_express_checkout)

    def test_buckaroo_provider_has_express_checkout_form_view(self):
        view = self.buckaroo.express_checkout_form_view_id
        self.assertTrue(view)
        self.assertEqual(
            view.xml_id,
            "payment_buckaroo_official.googlepay_express_form",
        )

    def test_buckaroo_provider_allows_express_checkout_by_default(self):
        self.assertTrue(self.buckaroo.allow_express_checkout)

    def test_other_provider_unaffected(self):
        # Pick any non-buckaroo provider record to ensure our hook
        # only flips the flag for buckaroo_official.
        other = self.env["payment.provider"].search(
            [("code", "!=", "buckaroo_official")],
            limit=1,
        )
        if other:
            self.assertFalse(other.support_express_checkout or False)


@tagged("post_install", "-at_install")
class TestGooglepayMethodExpressFlag(BuckarooOfficialCommon):
    def test_googlepay_method_supports_express_checkout(self):
        method = self.env.ref("payment_buckaroo_official.payment_method_googlepay")
        self.assertTrue(method.support_express_checkout)


@tagged("post_install", "-at_install")
class TestGooglepayExpressCompatFilter(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.googlepay = cls.env.ref("payment_buckaroo_official.payment_method_googlepay")
        cls.buckaroo.payment_method_ids = [Command.link(cls.googlepay.id)]

    def _express_compatible_codes(self):
        methods = self.env["payment.method"]._get_compatible_payment_methods(
            provider_ids=self.buckaroo.ids,
            partner_id=self.env.ref("base.partner_admin").id,
            currency_id=self.env.ref("base.EUR").id,
            is_express_checkout=True,
        )
        return set(methods.mapped("code"))

    def test_express_filter_keeps_configured_googlepay(self):
        self.googlepay.write(
            {
                "buckaroo_official_googlepay_merchant_guid": "BUCK-GUID-XYZ",
                "buckaroo_official_googlepay_show_on_cart": True,
            }
        )
        self.assertIn("googlepay", self._express_compatible_codes())

    def test_express_filter_drops_unconfigured_googlepay(self):
        self.googlepay.buckaroo_official_googlepay_merchant_guid = ""
        self.assertNotIn("googlepay", self._express_compatible_codes())

    def test_express_filter_drops_googlepay_in_prod_without_google_merchant_id(self):
        self.buckaroo.state = "enabled"
        self.googlepay.write(
            {
                "buckaroo_official_googlepay_merchant_guid": "BUCK-GUID-AAA",
                "buckaroo_official_googlepay_google_merchant_id": "",
            }
        )
        self.assertNotIn("googlepay", self._express_compatible_codes())

    def test_express_filter_drops_googlepay_when_show_on_cart_false(self):
        self.googlepay.write(
            {
                "buckaroo_official_googlepay_merchant_guid": "BUCK-GUID-XYZ",
                "buckaroo_official_googlepay_show_on_cart": False,
            }
        )
        self.assertNotIn("googlepay", self._express_compatible_codes())


@tagged("post_install", "-at_install")
class TestGooglepayCheckoutListCompatFilter(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.googlepay = cls.env.ref("payment_buckaroo_official.payment_method_googlepay")
        cls.buckaroo.payment_method_ids = [Command.link(cls.googlepay.id)]

    def _checkout_compatible_codes(self):
        methods = self.env["payment.method"]._get_compatible_payment_methods(
            provider_ids=self.buckaroo.ids,
            partner_id=self.env.ref("base.partner_admin").id,
            currency_id=self.env.ref("base.EUR").id,
            is_express_checkout=False,
        )
        return set(methods.mapped("code"))

    def _express_compatible_codes(self):
        methods = self.env["payment.method"]._get_compatible_payment_methods(
            provider_ids=self.buckaroo.ids,
            partner_id=self.env.ref("base.partner_admin").id,
            currency_id=self.env.ref("base.EUR").id,
            is_express_checkout=True,
        )
        return set(methods.mapped("code"))

    def test_show_on_checkout_true_keeps_googlepay_in_inline_list(self):
        self.googlepay.write(
            {
                "buckaroo_official_googlepay_merchant_guid": "BUCK-GUID-XYZ",
                "buckaroo_official_googlepay_show_on_checkout": True,
            }
        )
        self.assertIn("googlepay", self._checkout_compatible_codes())

    def test_show_on_checkout_false_drops_googlepay_from_inline_list(self):
        self.googlepay.write(
            {
                "buckaroo_official_googlepay_merchant_guid": "BUCK-GUID-XYZ",
                "buckaroo_official_googlepay_show_on_checkout": False,
            }
        )
        self.assertNotIn("googlepay", self._checkout_compatible_codes())

    def test_show_on_checkout_false_does_not_affect_express_cart_flow(self):
        self.googlepay.write(
            {
                "buckaroo_official_googlepay_merchant_guid": "BUCK-GUID-XYZ",
                "buckaroo_official_googlepay_show_on_checkout": False,
                "buckaroo_official_googlepay_show_on_cart": True,
            }
        )
        self.assertIn("googlepay", self._express_compatible_codes())


@tagged("post_install", "-at_install")
class TestGooglepayProductExpressInitController(BuckarooOfficialCommon):
    """Unit-tests for `/shop/buckaroo/googlepay/express_init`.

    The endpoint adds a product to the cart and returns the same payload
    `WebsiteSale._get_express_shop_payment_values` returns for the cart
    page, so the existing express flow can run from the product page.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.googlepay = cls.env.ref("payment_buckaroo_official.payment_method_googlepay")

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
        from ..controllers.express_googlepay import (
            BuckarooGooglepayExpressController,
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

        controller = BuckarooGooglepayExpressController()
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
                "odoo.addons.payment_buckaroo_official.controllers.express_googlepay.request",
                new=fake_req,
            ),
            patch.object(
                odoo.http,
                "request",
                fake_req,
            ),
            patch.object(
                BuckarooGooglepayExpressController,
                "add_to_cart",
                return_value={"cart_quantity": qty or 0},
            ) as add_to_cart_mock,
            patch.object(
                BuckarooGooglepayExpressController,
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
                result = controller.googlepay_express_init(**kwargs)
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
        # combination_id is not consumed by `_cart_add` and not part of
        # the controller signature. Calling the bare method with it must
        # raise TypeError; in production, Odoo's HTTP dispatcher silently
        # drops args that aren't in the signature.
        from ..controllers.express_googlepay import (
            BuckarooGooglepayExpressController,
        )
        import inspect

        sig = inspect.signature(BuckarooGooglepayExpressController.googlepay_express_init)
        self.assertNotIn("combination_id", sig.parameters)
        self.assertNotIn("product_template_id", sig.parameters)
        self.assertNotIn("kwargs", sig.parameters)

    def test_real_cart_add_charges_chosen_variant(self):
        # Integration test: hit the real `_cart_add` (no mock cart) with
        # a variant product and assert the resulting SOL has the variant
        # we asked for. Catches regressions where the endpoint silently
        # ignores variant params and charges the wrong product line.
        attribute = self.env["product.attribute"].create(
            {
                "name": "GP Test Color",
                "create_variant": "always",
                "value_ids": [
                    Command.create({"name": "GP Red"}),
                    Command.create({"name": "GP Blue"}),
                ],
            }
        )
        template = self.env["product.template"].create(
            {
                "name": "GP Test Variant Product",
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
        # Pick the "GP Blue" variant to ensure we're not just adding the
        # first/default variant.
        blue_variant = template.product_variant_ids.filtered(
            lambda v: any("GP Blue" in val.name for val in v.product_template_variant_value_ids)
        )
        self.assertTrue(blue_variant, "Expected a GP Blue variant.")

        website = self.env["website"].search([], limit=1)
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "website_id": website.id,
                "company_id": website.company_id.id,
            }
        )

        from ..controllers.express_googlepay import (
            BuckarooGooglepayExpressController,
        )

        controller = BuckarooGooglepayExpressController()

        fake_req = MagicMock()
        fake_req.cart = order
        fake_req.env = self.env
        fake_req.website = website

        import odoo.http

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.express_googlepay.request",
                new=fake_req,
            ),
            patch(
                "odoo.addons.website_sale.controllers.cart.request",
                new=fake_req,
            ),
            patch.object(
                odoo.http,
                "request",
                fake_req,
            ),
            patch.object(
                BuckarooGooglepayExpressController,
                "_get_express_shop_payment_values",
                return_value={"amount": order.amount_total},
            ),
        ):
            result = controller.googlepay_express_init(
                product_id=blue_variant.id,
                qty=3,
            )

        self.assertIsInstance(result, dict)
        self.assertEqual(len(order.order_line), 1)
        self.assertEqual(order.order_line.product_id, blue_variant)
        self.assertEqual(order.order_line.product_uom_qty, 3)

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
