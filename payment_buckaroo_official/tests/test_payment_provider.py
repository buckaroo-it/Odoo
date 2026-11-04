# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json
from unittest.mock import MagicMock, patch

import requests
from buckaroo.config.buckaroo_config import create_config_from_mode

import odoo.release
from odoo.exceptions import UserError
from odoo.modules.module import get_manifest
from odoo.tests import tagged

from odoo.addons.http_routing.tests.common import MockRequest
from odoo.addons.payment_buckaroo_official.utils import const
from odoo.addons.payment_buckaroo_official.models.payment_provider import (
    PaymentProvider,
    _buckaroo_official_software_header,
)
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
        provider = self.env.ref("payment_buckaroo_official.payment_provider_buckaroo_official")
        expected = "Your order has been received and is awaiting payment confirmation."
        self.assertIn(expected, provider.pending_msg)
        self.assertNotIn("waiting for approval", provider.pending_msg)
        self.assertEqual(provider._get_status_message("pending"), provider.pending_msg)

    def test_software_header_format(self):
        """The Software header is the JSON object the Buckaroo gateway requires."""
        plugin_version = get_manifest("payment_buckaroo_official")["version"]
        with patch.object(odoo.release, "version", "19.0-20260630"):
            header = json.loads(_buckaroo_official_software_header())
        self.assertEqual(
            header,
            {
                "PlatformName": "Odoo",
                "PlatformVersion": "19.0-20260630",
                "ModuleSupplier": "Buckaroo",
                "ModuleName": "Odoo",
                "ModuleVersion": plugin_version,
            },
        )

    def test_software_header_strips_saas_prefix(self):
        """Odoo Online reports ``saas~19.3``; the header shows plain ``19.3``."""
        with patch.object(odoo.release, "version", "saas~19.3"):
            header = json.loads(_buckaroo_official_software_header())
        self.assertEqual(header["PlatformVersion"], "19.3")

    def test_client_config_sends_software_header(self):
        """The client config carries the Software header alongside the SDK defaults."""
        headers = self.buckaroo._buckaroo_official_get_client().config.get_request_headers()
        self.assertEqual(headers["Software"], _buckaroo_official_software_header())
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_client_keeps_sdk_mode_presets(self):
        """Building the client from a config keeps the SDK's per-mode preset values."""
        for state, mode in (("test", "test"), ("enabled", "live")):
            with self.subTest(mode=mode):
                self.buckaroo.state = state
                config = self.buckaroo._buckaroo_official_get_client().config
                preset = create_config_from_mode(mode)
                for name in (
                    "environment",
                    "api_version",
                    "timeout",
                    "retry_attempts",
                    "retry_delay",
                    "logging_enabled",
                    "verify_ssl",
                    "custom_endpoint",
                    "user_agent",
                    "max_redirects",
                ):
                    self.assertEqual(getattr(config, name), getattr(preset, name), name)

    def test_software_header_is_sent_on_the_wire(self):
        """The header reaches the prepared request, not just the config."""
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        client = self.buckaroo._buckaroo_official_get_client()

        with patch.object(requests.Session, "send", return_value=response) as mock_send:
            client.confirm_credential()

        self.assertTrue(mock_send.called, "no HTTP request was attempted")
        prepared_request = mock_send.call_args[0][0]
        self.assertEqual(prepared_request.headers["Software"], _buckaroo_official_software_header())


@tagged("post_install", "-at_install")
class TestExpressProviderLookup(BuckarooOfficialCommon):
    """`_buckaroo_official_express_provider` picks the provider the product-page
    express buttons render with: scoped to the website and its company, and
    published unless an internal user is testing."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Only the test provider may match; other Buckaroo providers in the
        # database would otherwise win the `limit=1`.
        (
            cls.env["payment.provider"].search(
                [("code", "=", const.PROVIDER_CODE), ("id", "!=", cls.buckaroo.id)]
            )
        ).write({"state": "disabled"})
        cls.website = cls.env["website"].create(
            {"name": "Express Lookup", "company_id": cls.buckaroo.company_id.id}
        )
        cls.buckaroo.write(
            {
                "state": "test",
                "allow_express_checkout": True,
                "website_id": False,
                "is_published": False,
            }
        )

    def _lookup(self, website=None, user=None):
        env = self.env(user=user) if user else self.env
        return env["payment.provider"]._buckaroo_official_express_provider(website or self.website)

    def test_internal_user_sees_unpublished_provider(self):
        self.assertEqual(self._lookup(), self.buckaroo)

    def test_shopper_needs_a_published_provider(self):
        shopper = self.website.user_id
        self.assertFalse(self._lookup(user=shopper))
        self.buckaroo.is_published = True
        self.assertEqual(self._lookup(user=shopper), self.buckaroo)

    def test_provider_of_another_website_is_ignored(self):
        other = self.env["website"].create(
            {"name": "Other Site", "company_id": self.buckaroo.company_id.id}
        )
        self.buckaroo.website_id = other
        self.assertFalse(self._lookup())
        self.assertEqual(self._lookup(website=other), self.buckaroo)

    def test_provider_of_another_company_is_ignored(self):
        company = self.env["res.company"].create({"name": "Other Company"})
        other = self.env["website"].create({"name": "Other Co Site", "company_id": company.id})
        found = self._lookup(website=other)
        # Odoo may set up a provider for the new company; it never gets ours.
        self.assertNotEqual(found, self.buckaroo)
        if found:
            self.assertEqual(found.company_id, company)


@tagged("post_install", "-at_install")
class TestFrontendTranslations(BuckarooOfficialCommon):
    """Shop pages load JS translations only for the listed modules; ours must
    be one of them or NL/DE/FR shops show the wallet messages in English."""

    def test_module_js_strings_load_on_shop_pages(self):
        website = self.env["website"].search([], limit=1)
        with MockRequest(self.env, website=website):
            modules = self.env["ir.http"]._get_translation_frontend_modules_name()
        self.assertIn("payment_buckaroo_official", modules)
