# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Tests for the credit card payment method branch on payment.method.

Verifies the 4-way routing (redirect/inline x pay/authorize), hosted fields
session handling, token flows, and card brand injection for post-authorize
(capture/void) and refund requests.
"""

from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon, make_mock_sdk_builder


def _patch_request(session_dict):
    """Patch ``odoo.http.request`` with a mock carrying *session_dict*.

    The creditcard subclass uses a deferred ``from odoo.http import request``
    inside ``_buckaroo_create_payment``, so we patch the canonical location.
    """
    mock_req = MagicMock()
    mock_req.session = session_dict
    return patch("odoo.http.request", mock_req)


@tagged("post_install", "-at_install")
class TestCreditCardNonCreditCardUnaffected(BuckarooOfficialCommon):
    """Non-creditcard payment methods should pass through to super unchanged."""

    def test_ideal_calls_base_pay(self):
        """iDEAL (non-creditcard) should use the base _buckaroo_create_payment -> .pay()."""
        tx = self._create_buckaroo_tx(reference="TX-IDEAL-001", payment_method=self.ideal)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        mock_builder.authorize.assert_not_called()
        self.assertEqual(result, mock_response)


@tagged("post_install", "-at_install")
class TestCreditCardRedirectPay(BuckarooOfficialCommon):
    """Redirect + pay flow: no HF session data, authorize='pay'."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")
        cls.buckaroo.payment_method_ids = [Command.link(cls.creditcard.id)]
        cls.creditcard.buckaroo_official_creditcard_authorize = "pay"
        cls.creditcard.buckaroo_official_creditcard_method = "redirect"

    def test_redirect_pay_calls_sdk_pay(self):
        """Redirect + pay should call .pay() via base class."""
        tx = self._create_buckaroo_tx(reference="TX-CC-001", payment_method=self.creditcard)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_creditcard.PaymentService"
            ) as MockPS,
            _patch_request({}),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.creditcard._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        mock_builder.authorize.assert_not_called()
        mock_builder.payWithToken.assert_not_called()
        mock_builder.authorizeWithToken.assert_not_called()
        self.assertEqual(result, mock_response)


@tagged("post_install", "-at_install")
class TestCreditCardRedirectAuthorize(BuckarooOfficialCommon):
    """Redirect + authorize flow: no HF session data, authorize='authorize'."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")
        cls.buckaroo.payment_method_ids = [Command.link(cls.creditcard.id)]
        cls.creditcard.buckaroo_official_creditcard_authorize = "authorize"
        cls.creditcard.buckaroo_official_creditcard_method = "redirect"

    def test_redirect_authorize_calls_sdk_authorize(self):
        """Redirect + authorize should call .authorize()."""
        tx = self._create_buckaroo_tx(reference="TX-CC-AUTH-001", payment_method=self.creditcard)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_creditcard.PaymentService"
            ) as MockPS,
            _patch_request({}),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.creditcard._buckaroo_create_payment(tx, client)

        mock_builder.authorize.assert_called_once()
        mock_builder.pay.assert_not_called()
        mock_builder.payWithToken.assert_not_called()
        self.assertEqual(result, mock_response)


@tagged("post_install", "-at_install")
class TestCreditCardInlinePay(BuckarooOfficialCommon):
    """Inline (Hosted Fields) + pay flow: HF session data present, authorize='pay'."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")
        cls.buckaroo.payment_method_ids = [Command.link(cls.creditcard.id)]
        cls.creditcard.buckaroo_official_creditcard_authorize = "pay"

    def test_inline_pay_calls_pay_with_token(self):
        """HF session data + pay should call .payWithToken()."""
        tx = self._create_buckaroo_tx(reference="TX-CC-HF-001", payment_method=self.creditcard)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_creditcard.PaymentService"
            ) as MockPS,
            _patch_request(
                {
                    "buckaroo_hf_session_id": "hf-sess-123",
                    "buckaroo_hf_service": "visa",
                }
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.creditcard._buckaroo_create_payment(tx, client)

        mock_builder.payWithToken.assert_called_once()
        mock_builder.authorize.assert_not_called()
        mock_builder.authorizeWithToken.assert_not_called()
        self.assertEqual(result, mock_response)

    def test_inline_pay_adds_session_id_parameter(self):
        """HF session should add SessionId parameter to the builder."""
        tx = self._create_buckaroo_tx(reference="TX-CC-HF-002", payment_method=self.creditcard)
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_creditcard.PaymentService"
            ) as MockPS,
            _patch_request(
                {
                    "buckaroo_hf_session_id": "hf-sess-XYZ",
                    "buckaroo_hf_service": "mastercard",
                }
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            self.creditcard._buckaroo_create_payment(tx, client)

        mock_builder.add_parameter.assert_called_once_with("SessionId", "hf-sess-XYZ")

    def test_inline_pay_sets_brand_from_service(self):
        """HF session with service should set brand in params."""
        tx = self._create_buckaroo_tx(reference="TX-CC-HF-003", payment_method=self.creditcard)
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_creditcard.PaymentService"
            ) as MockPS,
            _patch_request(
                {
                    "buckaroo_hf_session_id": "hf-sess-456",
                    "buckaroo_hf_service": "visa",
                }
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            self.creditcard._buckaroo_create_payment(tx, client)

        params = MockPS.return_value.create_payment.call_args[0][1]
        self.assertEqual(params.get("brand"), "visa")

    def test_inline_pay_uses_creditcard_service_name(self):
        """HF token flow should use 'creditcard' as the SDK service name."""
        tx = self._create_buckaroo_tx(reference="TX-CC-HF-004", payment_method=self.creditcard)
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_creditcard.PaymentService"
            ) as MockPS,
            _patch_request(
                {
                    "buckaroo_hf_session_id": "hf-sess-789",
                    "buckaroo_hf_service": "amex",
                }
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            self.creditcard._buckaroo_create_payment(tx, client)

        service_arg = MockPS.return_value.create_payment.call_args[0][0]
        self.assertEqual(service_arg, "creditcard")

    def test_session_data_popped_after_use(self):
        """HF session keys should be popped (removed) from the HTTP session."""
        tx = self._create_buckaroo_tx(reference="TX-CC-HF-005", payment_method=self.creditcard)
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        session_dict = {
            "buckaroo_hf_session_id": "hf-sess-pop",
            "buckaroo_hf_service": "visa",
        }
        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_creditcard.PaymentService"
            ) as MockPS,
            _patch_request(session_dict),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            self.creditcard._buckaroo_create_payment(tx, client)

        self.assertNotIn("buckaroo_hf_session_id", session_dict)
        self.assertNotIn("buckaroo_hf_service", session_dict)


@tagged("post_install", "-at_install")
class TestCreditCardInlineAuthorize(BuckarooOfficialCommon):
    """Inline (Hosted Fields) + authorize flow."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")
        cls.buckaroo.payment_method_ids = [Command.link(cls.creditcard.id)]
        cls.creditcard.buckaroo_official_creditcard_authorize = "authorize"

    def test_inline_authorize_calls_authorize_with_token(self):
        """HF session data + authorize should call .authorizeWithToken()."""
        tx = self._create_buckaroo_tx(reference="TX-CC-HA-001", payment_method=self.creditcard)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_creditcard.PaymentService"
            ) as MockPS,
            _patch_request(
                {
                    "buckaroo_hf_session_id": "hf-sess-auth-456",
                    "buckaroo_hf_service": "mastercard",
                }
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.creditcard._buckaroo_create_payment(tx, client)

        mock_builder.authorizeWithToken.assert_called_once()
        mock_builder.payWithToken.assert_not_called()
        mock_builder.pay.assert_not_called()
        self.assertEqual(result, mock_response)


@tagged("post_install", "-at_install")
class TestCreditCardNoRequestContext(BuckarooOfficialCommon):
    """Credit card payment should work without an HTTP request context."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")
        cls.buckaroo.payment_method_ids = [Command.link(cls.creditcard.id)]
        cls.creditcard.buckaroo_official_creditcard_authorize = "pay"

    def test_no_request_falls_through_to_redirect_pay(self):
        """Without HTTP request, creditcard should fall through to redirect .pay()."""
        tx = self._create_buckaroo_tx(reference="TX-CC-NOREQ-001", payment_method=self.creditcard)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_creditcard.PaymentService"
            ) as MockPS,
            patch("odoo.http.request", None),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.creditcard._buckaroo_create_payment(tx, client)

        # Should fall through to base .pay(), not crash
        mock_builder.pay.assert_called_once()
        self.assertEqual(result, mock_response)

    def test_no_request_falls_through_to_redirect_authorize(self):
        """Without HTTP request, creditcard authorize should use redirect .authorize()."""
        self.creditcard.buckaroo_official_creditcard_authorize = "authorize"
        tx = self._create_buckaroo_tx(reference="TX-CC-NOREQ-AUTH", payment_method=self.creditcard)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_creditcard.PaymentService"
            ) as MockPS,
            patch("odoo.http.request", None),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.creditcard._buckaroo_create_payment(tx, client)

        mock_builder.authorize.assert_called_once()
        mock_builder.payWithToken.assert_not_called()
        self.assertEqual(result, mock_response)


@tagged("post_install", "-at_install")
class TestCreditCardGetPaymentAction(BuckarooOfficialCommon):
    """_buckaroo_get_payment_action override for creditcard."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")
        cls.buckaroo.payment_method_ids = [Command.link(cls.creditcard.id)]

    def test_returns_authorize_when_configured(self):
        """Creditcard with authorize setting should return 'authorize'."""
        self.creditcard.buckaroo_official_creditcard_authorize = "authorize"
        self.assertEqual(self.creditcard._buckaroo_get_payment_action(), "authorize")

    def test_returns_none_when_pay(self):
        """Creditcard with pay setting should return None (default flow)."""
        self.creditcard.buckaroo_official_creditcard_authorize = "pay"
        self.assertIsNone(self.creditcard._buckaroo_get_payment_action())

    def test_non_creditcard_returns_none(self):
        """Non-creditcard method should delegate to base and return None."""
        self.assertIsNone(self.ideal._buckaroo_get_payment_action())


@tagged("post_install", "-at_install")
class TestCreditCardPostAuthorizeParams(BuckarooOfficialCommon):
    """_buckaroo_get_post_authorize_params should add card brand for creditcard."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")
        cls.buckaroo.payment_method_ids = [Command.link(cls.creditcard.id)]

    def test_post_authorize_params_include_brand(self):
        """Capture/void params should include the card brand from service code."""
        tx = self._create_buckaroo_tx(reference="TX-CC-PA-001", payment_method=self.creditcard)
        tx.provider_reference = "AUTH_KEY_CC"
        tx.buckaroo_official_service_code = "visa"
        params, original_key = self.creditcard._buckaroo_get_post_authorize_params(tx)
        self.assertEqual(params["brand"], "visa")
        self.assertEqual(original_key, "AUTH_KEY_CC")

    def test_post_authorize_uses_source_tx_service_code(self):
        """When source_transaction_id is set, brand comes from source tx."""
        source_tx = self._create_buckaroo_tx(reference="SRC-CC-001", payment_method=self.creditcard)
        source_tx.provider_reference = "SRC_KEY_CC"
        source_tx.buckaroo_official_service_code = "mastercard"

        child_tx = self._create_buckaroo_tx(
            reference="CHILD-CC-001", payment_method=self.creditcard
        )
        child_tx.source_transaction_id = source_tx

        params, original_key = self.creditcard._buckaroo_get_post_authorize_params(child_tx)
        self.assertEqual(params["brand"], "mastercard")
        self.assertEqual(original_key, "SRC_KEY_CC")

    def test_post_authorize_raises_without_service_code(self):
        """Missing service code should raise ValidationError."""
        tx = self._create_buckaroo_tx(reference="TX-CC-PA-002", payment_method=self.creditcard)
        tx.provider_reference = "AUTH_KEY_NO_SC"
        tx.buckaroo_official_service_code = False

        with self.assertRaises(ValidationError):
            self.creditcard._buckaroo_get_post_authorize_params(tx)

    def test_ideal_post_authorize_has_no_brand(self):
        """iDEAL (non-creditcard) should NOT have brand in post-authorize params."""
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.ideal.id,
                "reference": "TX-IDEAL-PA",
                "amount": 50.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
            }
        )
        tx.provider_reference = "IDEAL_AUTH_KEY"
        params, original_key = self.ideal._buckaroo_get_post_authorize_params(tx)
        self.assertNotIn("brand", params)


@tagged("post_install", "-at_install")
class TestCreditCardRefundParams(BuckarooOfficialCommon):
    """_buckaroo_get_refund_params should add card brand for creditcard refunds."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")
        cls.buckaroo.payment_method_ids = [Command.link(cls.creditcard.id)]

    def _make_source_and_refund(self, reference_suffix, service_code="visa"):
        source_tx = self._create_buckaroo_tx(
            reference=f"SRC-CC-REF-{reference_suffix}",
            amount=50.0,
            payment_method=self.creditcard,
        )
        source_tx.provider_reference = f"SRC_KEY_{reference_suffix}"
        source_tx.buckaroo_official_service_code = service_code
        refund_tx = self._create_buckaroo_tx(
            reference=f"REF-CC-{reference_suffix}",
            amount=-50.0,
            payment_method=self.creditcard,
        )
        return source_tx, refund_tx

    def test_refund_params_include_brand_from_source(self):
        """Refund params should include the card brand from the source tx service code."""
        source_tx, refund_tx = self._make_source_and_refund("001", service_code="visa")
        params = self.creditcard._buckaroo_get_refund_params(source_tx, refund_tx)
        self.assertEqual(params["brand"], "visa")
        self.assertEqual(params["original_transaction_key"], "SRC_KEY_001")
        self.assertEqual(params["refund_amount"], 50.0)

    def test_refund_params_use_mastercard_brand(self):
        """Refund params should reflect a mastercard source."""
        source_tx, refund_tx = self._make_source_and_refund("002", service_code="mastercard")
        params = self.creditcard._buckaroo_get_refund_params(source_tx, refund_tx)
        self.assertEqual(params["brand"], "mastercard")

    def test_refund_raises_without_service_code(self):
        """Missing service code on source tx should raise ValidationError."""
        source_tx = self._create_buckaroo_tx(
            reference="SRC-CC-NOSC",
            amount=50.0,
            payment_method=self.creditcard,
        )
        source_tx.provider_reference = "SRC_KEY_NOSC"
        source_tx.buckaroo_official_service_code = False
        refund_tx = self._create_buckaroo_tx(
            reference="REF-CC-NOSC",
            amount=-50.0,
            payment_method=self.creditcard,
        )
        with self.assertRaises(ValidationError):
            self.creditcard._buckaroo_get_refund_params(source_tx, refund_tx)

    def test_refund_walks_source_chain_for_brand(self):
        """Refund off a capture tx must pull brand from the root authorize tx."""
        authorize_tx = self._create_buckaroo_tx(
            reference="SRC-CC-CHAIN-AUTH",
            amount=50.0,
            payment_method=self.creditcard,
        )
        authorize_tx.provider_reference = "AUTH_KEY_CHAIN"
        authorize_tx.buckaroo_official_service_code = "visa"
        capture_tx = self._create_buckaroo_tx(
            reference="P-SRC-CC-CHAIN",
            amount=50.0,
            payment_method=self.creditcard,
        )
        capture_tx.provider_reference = "CAP_KEY_CHAIN"
        capture_tx.source_transaction_id = authorize_tx.id
        # Capture tx itself has no service code; brand must come from authorize.
        self.assertFalse(capture_tx.buckaroo_official_service_code)
        refund_tx = self._create_buckaroo_tx(
            reference="R-P-SRC-CC-CHAIN",
            amount=-50.0,
            payment_method=self.creditcard,
        )
        params = self.creditcard._buckaroo_get_refund_params(capture_tx, refund_tx)
        self.assertEqual(params["brand"], "visa")

    def test_ideal_refund_has_no_brand(self):
        """Non-creditcard refund params should NOT include brand."""
        source_tx = self._create_buckaroo_tx(reference="SRC-IDEAL-REF", amount=50.0)
        source_tx.provider_reference = "SRC_IDEAL_KEY"
        refund_tx = self._create_buckaroo_tx(reference="REF-IDEAL-001", amount=-50.0)
        params = self.ideal._buckaroo_get_refund_params(source_tx, refund_tx)
        self.assertNotIn("brand", params)

    def test_create_refund_passes_brand_to_sdk(self):
        """_buckaroo_create_refund for creditcard sends brand in the SDK params."""
        source_tx, refund_tx = self._make_source_and_refund("SDK", service_code="amex")
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.creditcard._buckaroo_create_refund(source_tx, refund_tx, client)

        params = MockPS.return_value.create_payment.call_args[0][1]
        self.assertEqual(params["brand"], "amex")
        mock_builder.refund.assert_called_once()
        self.assertEqual(result, mock_response)
