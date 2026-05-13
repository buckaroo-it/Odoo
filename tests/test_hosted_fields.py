# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Tests for the Hosted Fields integration.

Covers:
- Provider settings (creditcard method, client ID, client secret fields)
- Transaction model (processing flow branching via request.session)
- JWT endpoint (route, token exchange, error handling)

SDK-level tests for CreditcardBuilder.payWithToken() live in
vendor/BuckarooSDK_Python/tests/test_credit_card_builder.py and are
intentionally not duplicated here.
"""

from unittest.mock import MagicMock, patch

from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon


@tagged("post_install", "-at_install")
class TestPaymentMethodHostedFieldsSettings(BuckarooOfficialCommon):
    """Verify that Hosted Fields configuration fields exist on payment.method and have correct defaults."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")
        cls.buckaroo.payment_method_ids = [Command.link(cls.creditcard.id)]
        # Reset to defaults so tests don't depend on DB state.
        cls.creditcard.write(
            {
                "buckaroo_official_creditcard_method": "redirect",
                "buckaroo_official_creditcard_authorize": "pay",
                "buckaroo_official_hosted_fields_client_id": False,
                "buckaroo_official_hosted_fields_client_secret": False,
            }
        )

    def test_creditcard_method_field_exists_with_default_redirect(self):
        self.assertEqual(
            self.creditcard.buckaroo_official_creditcard_method,
            "redirect",
        )

    def test_creditcard_method_accepts_inline(self):
        self.creditcard.buckaroo_official_creditcard_method = "inline"
        self.assertEqual(self.creditcard.buckaroo_official_creditcard_method, "inline")

    def test_creditcard_authorize_field_defaults_to_pay(self):
        self.assertEqual(
            self.creditcard.buckaroo_official_creditcard_authorize,
            "pay",
        )

    def test_creditcard_authorize_accepts_authorize(self):
        self.creditcard.buckaroo_official_creditcard_authorize = "authorize"
        self.assertEqual(self.creditcard.buckaroo_official_creditcard_authorize, "authorize")

    def test_hosted_fields_client_id_field_exists(self):
        self.assertFalse(self.creditcard.buckaroo_official_hosted_fields_client_id)
        self.creditcard.buckaroo_official_hosted_fields_client_id = "test_client_id"
        self.assertEqual(
            self.creditcard.buckaroo_official_hosted_fields_client_id,
            "test_client_id",
        )

    def test_hosted_fields_client_secret_field_exists(self):
        self.assertFalse(self.creditcard.buckaroo_official_hosted_fields_client_secret)
        self.creditcard.buckaroo_official_hosted_fields_client_secret = "test_secret"
        self.assertEqual(
            self.creditcard.buckaroo_official_hosted_fields_client_secret,
            "test_secret",
        )


@tagged("post_install", "-at_install")
class TestTransactionHostedFields(BuckarooOfficialCommon):
    """Verify hosted fields fields and processing flow branching on payment.transaction."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")
        cls.buckaroo.payment_method_ids = [Command.link(cls.creditcard.id)]
        # Reset to defaults so tests don't depend on state left by sibling classes.
        cls.creditcard.write(
            {
                "buckaroo_official_creditcard_method": "redirect",
                "buckaroo_official_creditcard_authorize": "pay",
            }
        )
        cls.creditcard_tx_values = dict(cls.buckaroo_tx_values)
        cls.creditcard_tx_values["payment_method_id"] = cls.creditcard.id

    def test_processing_uses_redirect_when_no_session_in_http_session(self):
        """Without session data, _get_specific_processing_values uses the redirect flow."""
        tx = self.env["payment.transaction"].create(self.buckaroo_tx_values)

        mock_response = MagicMock()
        mock_response.status_code = 190
        mock_response.key = "TXN_KEY"
        mock_response.redirect_url = "https://checkout.buckaroo.nl/pay"
        mock_response.required_action = None
        mock_response.buckaroo_status_message = None
        mock_response._raw_data = {}
        mock_response.get_redirect_url.return_value = "https://checkout.buckaroo.nl/pay"

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
            ) as MockPS,
            patch.object(type(tx), "_buckaroo_official_get_client", return_value=MagicMock()),
            patch.object(
                type(tx.payment_method_id),
                "_buckaroo_official_is_amount_compatible",
                return_value=True,
            ),
        ):
            mock_builder = MagicMock()
            mock_builder.pay.return_value = mock_response
            MockPS.return_value.create_payment.return_value = mock_builder

            result = tx._get_specific_processing_values({})

        mock_builder.pay.assert_called_once()
        self.assertEqual(result["api_url"], "https://checkout.buckaroo.nl/pay")

    def test_processing_uses_hosted_fields_when_session_data_present(self):
        """With HF data in the HTTP session, create_payment uses PayWithToken flow."""
        tx = self.env["payment.transaction"].create(self.creditcard_tx_values)

        mock_response = MagicMock()
        mock_response.status_code = 190
        mock_response.key = "TXN_KEY_HF"
        mock_response.redirect_url = "https://checkout.buckaroo.nl/3ds"
        mock_response.required_action = None
        mock_response.buckaroo_status_message = None
        mock_response._raw_data = {}
        mock_response.get_redirect_url.return_value = "https://checkout.buckaroo.nl/3ds"

        mock_session = {
            "buckaroo_hf_session_id": "hf-session-token",
            "buckaroo_hf_service": "visa",
        }

        import odoo.http

        mock_request = MagicMock()
        mock_request.session = mock_session

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_creditcard.PaymentService"
            ) as MockPS,
            patch.object(
                odoo.http,
                "request",
                mock_request,
            ),
            patch.object(type(tx), "_buckaroo_official_get_client", return_value=MagicMock()),
            patch.object(
                type(tx.payment_method_id),
                "_buckaroo_official_is_amount_compatible",
                return_value=True,
            ),
        ):
            mock_builder = MagicMock()
            mock_builder.payWithToken.return_value = mock_response
            MockPS.return_value.create_payment.return_value = mock_builder

            result = tx._get_specific_processing_values({})

        mock_builder.payWithToken.assert_called_once()
        mock_builder.add_parameter.assert_called_once_with("SessionId", "hf-session-token")
        self.assertEqual(result["api_url"], "https://checkout.buckaroo.nl/3ds")


@tagged("post_install", "-at_install")
class TestHostedFieldsTokenEndpoint(BuckarooOfficialCommon):
    """Test the /payment/buckaroo_official/hosted-fields-token JSON endpoint."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")
        cls.buckaroo.payment_method_ids = [Command.link(cls.creditcard.id)]

    def _make_controller(self):
        """Instantiate the CreditCardController."""
        from odoo.addons.payment_buckaroo_official.controllers.creditcard import (
            CreditCardController,
        )

        return CreditCardController()

    def _make_request_mock(self):
        """Build a MagicMock that stands in for ``odoo.http.request``.

        Its ``env`` is the test env so controller code paths that consult
        ``request.env`` — including Odoo's ``_()`` helper via
        ``odoo.http.request`` — resolve to real model records and a real
        language context.
        """
        mock_request = MagicMock()
        mock_request.env = self.env
        return mock_request

    def test_endpoint_route_exists(self):
        """The hosted fields token URL is registered as a controller route."""
        controller = self._make_controller()
        method = getattr(controller, "hosted_fields_token", None)
        self.assertIsNotNone(
            method, "hosted_fields_token method must exist on CreditCardController"
        )

    def test_token_exchange_success(self):
        """Successful OAuth token exchange returns access_token and expires_in."""
        import odoo.http

        self.creditcard.buckaroo_official_hosted_fields_client_id = "my_client_id"
        self.creditcard.buckaroo_official_hosted_fields_client_secret = "my_client_secret"

        mock_oauth = MagicMock()
        mock_oauth.get_token.return_value = {
            "access_token": "jwt-token-abc",
            "expires_in": 3600,
        }

        controller = self._make_controller()
        mock_request = self._make_request_mock()

        with (
            patch.object(
                type(self.env["payment.provider"]),
                "sudo",
                return_value=self.env["payment.provider"],
            ),
            patch.object(
                type(self.env["payment.method"]),
                "sudo",
                return_value=self.env["payment.method"],
            ),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.creditcard.HostedFieldsService",
                return_value=mock_oauth,
            ) as mock_cls,
            patch.object(
                odoo.http,
                "request",
                mock_request,
            ),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.creditcard.request",
                new=mock_request,
            ),
        ):
            result = controller.hosted_fields_token(
                provider_id=self.buckaroo.id,
                payment_method_id=self.creditcard.id,
            )

        mock_cls.assert_called_once_with("my_client_id", "my_client_secret")
        mock_oauth.get_token.assert_called_once_with()
        self.assertEqual(result["access_token"], "jwt-token-abc")
        self.assertEqual(result["expires_in"], 3600)

    def test_token_exchange_missing_credentials(self):
        """When client ID/secret are missing, the endpoint returns an error."""
        import odoo.http

        self.creditcard.buckaroo_official_hosted_fields_client_id = False
        self.creditcard.buckaroo_official_hosted_fields_client_secret = False

        controller = self._make_controller()
        mock_request = self._make_request_mock()

        with (
            patch.object(odoo.http, "request", mock_request),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.creditcard.request",
                new=mock_request,
            ),
        ):
            result = controller.hosted_fields_token(
                provider_id=self.buckaroo.id,
                payment_method_id=self.creditcard.id,
            )

        self.assertIn("error", result)
        self.assertIn("not configured", result["error"])

    def test_token_exchange_request_failure(self):
        """When the OAuth token request fails, the endpoint returns an error."""
        import odoo.http
        from buckaroo.exceptions._buckaroo_error import BuckarooError

        self.creditcard.buckaroo_official_hosted_fields_client_id = "my_client_id"
        self.creditcard.buckaroo_official_hosted_fields_client_secret = "my_client_secret"

        controller = self._make_controller()
        mock_request = self._make_request_mock()

        mock_oauth = MagicMock()
        mock_oauth.get_token.side_effect = BuckarooError("Connection refused")

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.creditcard.HostedFieldsService",
                return_value=mock_oauth,
            ),
            patch.object(odoo.http, "request", mock_request),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.creditcard.request",
                new=mock_request,
            ),
        ):
            result = controller.hosted_fields_token(
                provider_id=self.buckaroo.id,
                payment_method_id=self.creditcard.id,
            )

        self.assertIn("error", result)
        self.assertIn("Failed", result["error"])

    def test_token_exchange_invalid_provider(self):
        """An invalid provider ID returns an error."""
        import odoo.http

        controller = self._make_controller()
        mock_request = self._make_request_mock()

        with (
            patch.object(odoo.http, "request", mock_request),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.creditcard.request",
                new=mock_request,
            ),
        ):
            result = controller.hosted_fields_token(
                provider_id=999999,
            )

        self.assertIn("error", result)
        self.assertIn("Invalid", result["error"])

    def test_token_exchange_unknown_payment_method_returns_error(self):
        """A non-existing payment_method_id returns an error mentioning the method."""
        import odoo.http

        controller = self._make_controller()
        mock_request = self._make_request_mock()

        with (
            patch.object(odoo.http, "request", mock_request),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.creditcard.request",
                new=mock_request,
            ),
        ):
            result = controller.hosted_fields_token(
                provider_id=self.buckaroo.id,
                payment_method_id=999999,
            )

        self.assertIn("error", result)
        self.assertIn("not found", result["error"])

    def test_token_exchange_non_creditcard_method_returns_error(self):
        """A payment_method_id pointing to a non-creditcard method returns an error."""
        import odoo.http

        # ``self.ideal`` is already linked to the provider via
        # ``BuckarooOfficialCommon.setUpClass``; linking it again would be a
        # no-op. We just reuse it here to trigger the non-creditcard branch.
        ideal = self.ideal

        controller = self._make_controller()
        mock_request = self._make_request_mock()

        with (
            patch.object(odoo.http, "request", mock_request),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.creditcard.request",
                new=mock_request,
            ),
        ):
            result = controller.hosted_fields_token(
                provider_id=self.buckaroo.id,
                payment_method_id=ideal.id,
            )

        self.assertIn("error", result)
        self.assertIn("not a credit card", result["error"])


@tagged("post_install", "-at_install")
class TestSdkServiceNameField(BuckarooOfficialCommon):
    """Verify the buckaroo_official_sdk_service_name Char field on payment.method."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")

    def test_sdk_service_name_field_exists(self):
        self.assertIn(
            "buckaroo_official_sdk_service_name",
            self.creditcard._fields,
        )

    def test_sdk_service_name_is_char(self):
        field = self.creditcard._fields["buckaroo_official_sdk_service_name"]
        self.assertEqual(field.type, "char")

    def test_creditcard_has_sdk_service_name(self):
        self.assertEqual(self.creditcard.buckaroo_official_sdk_service_name, "creditcard")


@tagged("post_install", "-at_install")
class TestCreditCardBrandMethods(BuckarooOfficialCommon):
    """Verify that all 11 credit card brand child payment.method records exist."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")

    def test_11_brand_child_methods_exist(self):
        children = self.env["payment.method"].search(
            [
                ("primary_payment_method_id", "=", self.creditcard.id),
            ]
        )
        self.assertEqual(len(children), 11)

    def test_each_brand_has_sdk_service_name(self):
        children = self.env["payment.method"].search(
            [
                ("primary_payment_method_id", "=", self.creditcard.id),
            ]
        )
        for child in children:
            with self.subTest(brand=child.code):
                self.assertTrue(
                    child.buckaroo_official_sdk_service_name,
                    "Brand %s has no sdk_service_name" % child.code,
                )

    def test_expected_brand_codes(self):
        children = self.env["payment.method"].search(
            [
                ("primary_payment_method_id", "=", self.creditcard.id),
            ]
        )
        codes = set(children.mapped("code"))
        expected = {
            "visa",
            "mastercard",
            "amex",
            "maestro",
            "visaelectron",
            "vpay",
            "cartebancaire",
            "cartebleuevisa",
            "dankort",
            "nexi",
            "postepay",
        }
        self.assertEqual(codes, expected)
