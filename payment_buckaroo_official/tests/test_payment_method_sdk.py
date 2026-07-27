# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Tests for _buckaroo_* SDK methods on the payment.method Odoo model."""

from unittest.mock import MagicMock, patch

from odoo.tests import tagged

from .common import BuckarooOfficialCommon, make_mock_response, make_mock_sdk_builder


@tagged("post_install", "-at_install")
class TestBuckarooGetPaymentAction(BuckarooOfficialCommon):
    """Test _buckaroo_get_payment_action on payment.method model."""

    def test_base_method_returns_none(self):
        """The base payment method should return None (default pay flow)."""
        self.assertIsNone(self.ideal._buckaroo_get_payment_action())


@tagged("post_install", "-at_install")
class TestBuckarooResolveDescription(BuckarooOfficialCommon):
    """Test _buckaroo_resolve_description static helper."""

    def test_empty_template_returns_reference(self):
        tx = self._create_buckaroo_tx(reference="TX-100")
        result = self.ideal._buckaroo_resolve_description(False, tx)
        self.assertEqual(result, "TX-100")

    def test_order_number_placeholder(self):
        tx = self._create_buckaroo_tx(reference="TX-200")
        result = self.ideal._buckaroo_resolve_description("Order {order_number}", tx)
        self.assertEqual(result, "Order TX-200")

    def test_shop_name_placeholder(self):
        tx = self._create_buckaroo_tx(reference="TX-300")
        result = self.ideal._buckaroo_resolve_description("{shop_name}", tx)
        self.assertEqual(result, tx.company_id.name)

    def test_unknown_placeholder_stripped(self):
        """Template with only an unknown placeholder resolves to tx.reference."""
        tx = self._create_buckaroo_tx(reference="TX-400")
        result = self.ideal._buckaroo_resolve_description("{unknown}", tx)
        self.assertEqual(result, "TX-400")

    def test_mixed_known_and_unknown_placeholders(self):
        """Known placeholders kept, unknown ones stripped."""
        tx = self._create_buckaroo_tx(reference="TX-500")
        result = self.ideal._buckaroo_resolve_description("Paid {order_number} ref {unknown}", tx)
        self.assertEqual(result, "Paid TX-500 ref")


@tagged("post_install", "-at_install")
class TestBuckarooGetPaymentParams(BuckarooOfficialCommon):
    """Test _buckaroo_get_payment_params on payment.method model."""

    def test_returns_dict_with_required_keys(self):
        tx = self._create_buckaroo_tx()
        params = self.ideal._buckaroo_get_payment_params(tx)
        expected_keys = {
            "currency",
            "amount",
            "description",
            "invoice",
            "return_url",
            "return_url_cancel",
            "return_url_error",
            "return_url_reject",
            "push_url",
            "push_url_failure",
        }
        self.assertEqual(set(params.keys()), expected_keys)

    def test_amount_and_currency(self):
        tx = self._create_buckaroo_tx(amount=99.95)
        params = self.ideal._buckaroo_get_payment_params(tx)
        self.assertEqual(params["amount"], 99.95)
        self.assertEqual(params["currency"], "EUR")

    def test_invoice_is_reference(self):
        tx = self._create_buckaroo_tx(reference="REF-42")
        params = self.ideal._buckaroo_get_payment_params(tx)
        self.assertEqual(params["invoice"], "REF-42")

    def test_urls_contain_buckaroo_paths(self):
        tx = self._create_buckaroo_tx()
        params = self.ideal._buckaroo_get_payment_params(tx)
        self.assertIn("/payment/buckaroo_official/return", params["return_url"])
        self.assertIn("/payment/buckaroo_official/webhook", params["push_url"])

    def test_description_uses_template_when_set(self):
        self.buckaroo.buckaroo_official_transaction_description = "Pay {order_number}"
        tx = self._create_buckaroo_tx(reference="TX-DESC")
        params = self.ideal._buckaroo_get_payment_params(tx)
        self.assertEqual(params["description"], "Pay TX-DESC")


@tagged("post_install", "-at_install")
class TestBuckarooCreatePayment(BuckarooOfficialCommon):
    """Test _buckaroo_create_payment on payment.method model."""

    def test_calls_sdk_pay(self):
        tx = self._create_buckaroo_tx()
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_payment(tx, client)

        MockPS.assert_called_once_with(client)
        mock_builder.pay.assert_called_once()
        self.assertEqual(result, mock_response)

    def test_uses_correct_service_name(self):
        tx = self._create_buckaroo_tx()
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.ideal._buckaroo_create_payment(tx, client)

        service_arg = MockPS.return_value.create_payment.call_args[0][0]
        self.assertEqual(service_arg, "ideal")


@tagged("post_install", "-at_install")
class TestBuckarooRefund(BuckarooOfficialCommon):
    """Test _buckaroo_get_refund_params and _buckaroo_create_refund."""

    def test_refund_params_include_original_key(self):
        source_tx = self._create_buckaroo_tx(reference="ORIG-001", amount=50.0)
        source_tx.provider_reference = "ORIG_TXN_KEY"
        refund_tx = self._create_buckaroo_tx(reference="REF-001", amount=-50.0)
        params = self.ideal._buckaroo_get_refund_params(source_tx, refund_tx)
        self.assertEqual(params["original_transaction_key"], "ORIG_TXN_KEY")
        self.assertEqual(params["refund_amount"], 50.0)

    def test_refund_uses_refund_description(self):
        """Refund params use refund_description when set on provider."""
        self.buckaroo.buckaroo_official_refund_description = "Refund {order_number}"
        self.buckaroo.buckaroo_official_transaction_description = "Pay {order_number}"
        source_tx = self._create_buckaroo_tx(reference="ORIG-R1", amount=30.0)
        source_tx.provider_reference = "ORIG_R1_KEY"
        refund_tx = self._create_buckaroo_tx(reference="REF-R1", amount=-30.0)
        params = self.ideal._buckaroo_get_refund_params(source_tx, refund_tx)
        self.assertEqual(params["description"], "Refund REF-R1")

    def test_refund_falls_back_to_transaction_description(self):
        """Empty refund_description falls back to transaction_description."""
        self.buckaroo.buckaroo_official_refund_description = False
        self.buckaroo.buckaroo_official_transaction_description = "Order {order_number}"
        source_tx = self._create_buckaroo_tx(reference="ORIG-R2", amount=40.0)
        source_tx.provider_reference = "ORIG_R2_KEY"
        refund_tx = self._create_buckaroo_tx(reference="REF-R2", amount=-40.0)
        params = self.ideal._buckaroo_get_refund_params(source_tx, refund_tx)
        self.assertEqual(params["description"], "Order REF-R2")

    def test_create_refund_calls_sdk_refund(self):
        source_tx = self._create_buckaroo_tx(reference="ORIG-002", amount=50.0)
        source_tx.provider_reference = "ORIG_KEY_2"
        refund_tx = self._create_buckaroo_tx(reference="REF-002", amount=-25.0)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_refund(source_tx, refund_tx, client)

        mock_builder.refund.assert_called_once()
        mock_builder.pay.assert_not_called()
        self.assertEqual(result, mock_response)

    def test_refund_after_capture_uses_capture_key(self):
        """When the source tx was authorized and later captured, the
        refund's ``original_transaction_key`` must be the capture
        child's provider_reference — not the auth's. Buckaroo rejects
        the auth key with 490 "Invalid parameter: originaltransaction".
        """
        auth_tx = self._create_buckaroo_tx(reference="AUTH-001", amount=63.0)
        auth_tx.provider_reference = "AUTH_KEY"
        auth_tx.buckaroo_official_payment_action = "authorize"
        # Simulate Odoo's _capture(): child carries parent's operation,
        # transitions through authorized → done as Buckaroo confirms.
        capture_tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.ideal.id,
                "reference": "P-AUTH-001",
                "amount": 63.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
                "source_transaction_id": auth_tx.id,
                "provider_reference": "CAPTURE_KEY",
            }
        )
        capture_tx._set_done()
        auth_tx._set_done()

        refund_tx = self._create_buckaroo_tx(reference="R-AUTH-001", amount=-63.0)
        params = self.ideal._buckaroo_get_refund_params(auth_tx, refund_tx)
        self.assertEqual(params["original_transaction_key"], "CAPTURE_KEY")

    def test_refund_without_capture_child_uses_source_key(self):
        """Direct-pay refund (no capture child) must keep using the
        source tx's provider_reference — guards the W6 direct-pay path.
        """
        source_tx = self._create_buckaroo_tx(reference="DP-001", amount=50.0)
        source_tx.provider_reference = "DIRECT_PAY_KEY"
        refund_tx = self._create_buckaroo_tx(reference="R-DP-001", amount=-50.0)
        params = self.ideal._buckaroo_get_refund_params(source_tx, refund_tx)
        self.assertEqual(params["original_transaction_key"], "DIRECT_PAY_KEY")

    def test_refund_ignores_refund_children_when_resolving_key(self):
        """A prior refund child must not be mistaken for a capture
        child — its operation is ``refund``, not the parent's.
        """
        source_tx = self._create_buckaroo_tx(reference="MIX-001", amount=50.0)
        source_tx.provider_reference = "SRC_KEY"
        prior_refund = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.ideal.id,
                "reference": "R-MIX-001",
                "amount": -25.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner.id,
                "operation": "refund",
                "source_transaction_id": source_tx.id,
                "provider_reference": "PRIOR_REFUND_KEY",
            }
        )
        prior_refund._set_done()

        new_refund = self._create_buckaroo_tx(reference="R-MIX-002", amount=-25.0)
        params = self.ideal._buckaroo_get_refund_params(source_tx, new_refund)
        self.assertEqual(params["original_transaction_key"], "SRC_KEY")


@tagged("post_install", "-at_install")
class TestBuckarooPostAuthorize(BuckarooOfficialCommon):
    """Test _buckaroo_create_capture and _buckaroo_create_void."""

    def test_capture_calls_sdk_capture(self):
        tx = self._create_buckaroo_tx()
        tx.provider_reference = "AUTH_KEY"
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_capture(tx, client)

        mock_builder.capture.assert_called_once()
        self.assertEqual(result, mock_response)

    def test_void_calls_sdk_cancel_authorize(self):
        tx = self._create_buckaroo_tx()
        tx.provider_reference = "AUTH_KEY"
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_void(tx, client)

        mock_builder.cancelAuthorize.assert_called_once()
        self.assertEqual(result, mock_response)

    def test_post_authorize_params_include_original_key(self):
        tx = self._create_buckaroo_tx()
        tx.provider_reference = "AUTH_KEY_123"
        params, original_key = self.ideal._buckaroo_get_post_authorize_params(tx)
        self.assertEqual(original_key, "AUTH_KEY_123")
        self.assertNotIn("original_transaction_key", params)

    def test_post_authorize_uses_source_tx_when_available(self):
        source_tx = self._create_buckaroo_tx(reference="SRC-001")
        source_tx.provider_reference = "SRC_KEY"
        child_tx = self._create_buckaroo_tx(reference="CHILD-001")
        child_tx.source_transaction_id = source_tx
        params, original_key = self.ideal._buckaroo_get_post_authorize_params(child_tx)
        self.assertEqual(original_key, "SRC_KEY")
        self.assertNotIn("original_transaction_key", params)


@tagged("post_install", "-at_install")
class TestBuckarooExtractRedirectUrl(BuckarooOfficialCommon):
    """Test _buckaroo_extract_redirect_url."""

    def test_returns_redirect_url_from_response(self):
        response = make_mock_response("https://checkout.buckaroo.nl/x")
        url = self.ideal._buckaroo_extract_redirect_url(response)
        self.assertEqual(url, "https://checkout.buckaroo.nl/x")

    def test_falls_back_to_required_action(self):
        response = MagicMock()
        response.get_redirect_url.return_value = None
        response.required_action.redirect_url = "https://fallback.url/pay"
        url = self.ideal._buckaroo_extract_redirect_url(response)
        self.assertEqual(url, "https://fallback.url/pay")

    def test_returns_none_when_no_url(self):
        response = MagicMock()
        response.get_redirect_url.return_value = None
        response.required_action = None
        url = self.ideal._buckaroo_extract_redirect_url(response)
        self.assertIsNone(url)


@tagged("post_install", "-at_install")
class TestProcessingValuesSurfacesSdkError(BuckarooOfficialCommon):
    """When the SDK response carries no redirect URL but the response
    exposes an error via ``response.get_some_error()`` (SDK helper that
    walks RequestErrors → ConsumerMessage → Message → SubCode), the
    transaction-creation path lifts that text into the
    ``ValidationError`` shown to the shopper."""

    def test_processing_values_raises_with_sdk_error_text(self):
        riverty_msg = (
            "Authorize rejected. The following errors occurred: File format is not supported."
        )
        response = MagicMock()
        response.get_redirect_url.return_value = None
        response.required_action = None
        response.key = "TXN"
        response.status_code = 200
        response.redirect_url = None
        response._raw_data = {}
        response.get_some_error.return_value = riverty_msg
        response.buckaroo_status_message = "Validation failure"

        tx = self._create_buckaroo_tx(reference="TX-RV-491")
        PaymentMethod = type(tx.payment_method_id)
        with (
            patch.object(PaymentMethod, "_buckaroo_create_payment", return_value=response),
            patch.object(PaymentMethod, "_buckaroo_extract_redirect_url", return_value=None),
        ):
            from odoo.exceptions import ValidationError  # noqa: PLC0415

            with self.assertRaises(ValidationError) as ctx:
                tx._get_specific_processing_values({})
        self.assertIn(riverty_msg, str(ctx.exception))

    def test_processing_values_raises_generic_when_sdk_has_no_error(self):
        response = MagicMock()
        response.get_redirect_url.return_value = None
        response.required_action = None
        response.key = "TXN"
        response.status_code = 200
        response.redirect_url = None
        response._raw_data = {}
        response.get_some_error.return_value = ""
        response.buckaroo_status_message = None

        tx = self._create_buckaroo_tx(reference="TX-RV-NOERR")
        PaymentMethod = type(tx.payment_method_id)
        with (
            patch.object(PaymentMethod, "_buckaroo_create_payment", return_value=response),
            patch.object(PaymentMethod, "_buckaroo_extract_redirect_url", return_value=None),
        ):
            from odoo.exceptions import ValidationError  # noqa: PLC0415

            with self.assertRaises(ValidationError) as ctx:
                tx._get_specific_processing_values({})
        # Generic fallback message lands when SDK has nothing useful.
        self.assertIn("payment could not be initiated", str(ctx.exception))
