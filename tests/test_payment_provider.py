# Part of Odoo. See LICENSE file for full copyright and licensing details.

from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.payment_buckaroo_official.utils import const
from odoo.addons.payment_buckaroo_official.models.payment_provider import PaymentProvider
from .common import BuckarooOfficialCommon


@tagged("post_install", "-at_install")
class TestPaymentProvider(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def test_provider_configuration_and_capabilities(self):
        self.assertTrue(self.buckaroo, "Buckaroo Official provider should exist after module setup")
        self.assertEqual(self.buckaroo.code, "buckaroo_official")
        self.assertEqual(
            self.buckaroo._get_default_payment_method_codes(),
            const.DEFAULT_PAYMENT_METHOD_CODES,
        )
        self.assertIn("EUR", self.buckaroo._get_supported_currencies().mapped("name"))
        self.assertEqual(self.buckaroo.buckaroo_official_website_key, "test_website_key")
        self.assertEqual(self.buckaroo.buckaroo_official_secret_key, "test_secret_key")

    def test_provider_supports_manual_capture(self):
        """Buckaroo Official provider should support manual capture (full_only)."""
        self.buckaroo._compute_feature_support_fields()
        self.assertEqual(self.buckaroo.support_manual_capture, "full_only")

    def test_provider_supports_partial_refund(self):
        """Buckaroo Official provider should support partial refund."""
        self.buckaroo._compute_feature_support_fields()
        self.assertEqual(self.buckaroo.support_refund, "partial")

    @patch.object(PaymentProvider, "_buckaroo_official_get_client")
    def test_test_connection_failure_raises(self, mock_get_client):
        """Test connection raises UserError when confirm_credential returns False."""
        mock_client = MagicMock()
        mock_client.confirm_credential.return_value = False
        mock_get_client.return_value = mock_client

        with self.assertRaises(UserError):
            self.buckaroo.action_buckaroo_official_test_connection()

    @patch.object(PaymentProvider, "_buckaroo_official_get_client")
    def test_test_connection_success(self, mock_get_client):
        """Successful test connection returns a display_notification action with type='success'."""
        mock_client = MagicMock()
        mock_client.confirm_credential.return_value = True
        mock_get_client.return_value = mock_client

        result = self.buckaroo.action_buckaroo_official_test_connection()

        self.assertEqual(result["type"], "ir.actions.client")
        self.assertEqual(result["tag"], "display_notification")
        self.assertEqual(result["params"]["type"], "success")

    def test_get_client_on_disabled_provider_raises(self):
        """Requesting an SDK client on a disabled provider raises UserError with a
        message mentioning the disabled state."""
        self.buckaroo.state = "disabled"
        with self.assertRaises(UserError) as ctx:
            self.buckaroo._buckaroo_official_get_client()
        self.assertIn("disabled", str(ctx.exception).lower())

    def test_default_pending_message(self):
        """Buckaroo ships the new default pending message, not the base one."""
        provider = self.env.ref(
            "payment_buckaroo_official.payment_provider_buckaroo_official"
        )
        expected = "Your order has been received and is awaiting payment confirmation."
        self.assertIn(expected, provider.pending_msg)
        self.assertNotIn("waiting for approval", provider.pending_msg)
        self.assertEqual(provider._get_status_message("pending"), provider.pending_msg)
