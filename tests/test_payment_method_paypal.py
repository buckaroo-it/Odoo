# Part of Odoo. See LICENSE file for full copyright and licensing details.

from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import tagged

from .common import (
    BuckarooOfficialCommon,
    make_mock_sdk_builder,
    make_mock_sdk_response,
    parsed_from_form,
)

_PAYPAL_PS = "odoo.addons.payment_buckaroo_official.models.payment_method_paypal.PaymentService"


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


def _paypal_push(**params):
    """Build a ParsedPush carrying PayPal Express service params via the real parser."""
    raw = {
        "brq_invoicenumber": "TX-PP-PUSH",
        "brq_statuscode": "190",
        "brq_transaction_method": "paypal",
    }
    raw.update({f"brq_service_paypal_{k}": v for k, v in params.items()})
    return parsed_from_form(raw)


@tagged("post_install", "-at_install")
class TestPaypalCreatePayment(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.paypal = cls.env.ref("payment_buckaroo_official.payment_method_paypal")
        cls.buckaroo.payment_method_ids = [Command.link(cls.paypal.id)]

    def test_express_payment_sends_paypal_order_id_from_session(self):
        tx = self._create_buckaroo_tx(reference="TX-PP-001", payment_method=self.paypal)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with (
            patch(_PAYPAL_PS) as MockPS,
            _patch_request({"buckaroo_paypal_order_id": "PP-ORDER-XYZ"}),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.paypal._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        self.assertEqual(result, mock_response)

        param_calls = {
            call.args[0]: call.args[1] for call in mock_builder.add_parameter.call_args_list
        }
        self.assertEqual(param_calls.get("payPalOrderId"), "PP-ORDER-XYZ")
        self.assertEqual(MockPS.return_value.create_payment.call_args[0][0], "paypal")
        self.assertTrue(tx.buckaroo_official_is_paypal_express)

    def test_express_payment_pops_session_order_id(self):
        tx = self._create_buckaroo_tx(reference="TX-PP-POP", payment_method=self.paypal)
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()
        session = {"buckaroo_paypal_order_id": "PP-ORDER-POP"}

        with patch(_PAYPAL_PS) as MockPS, _patch_request(session):
            MockPS.return_value.create_payment.return_value = mock_builder
            self.paypal._buckaroo_create_payment(tx, client)

        self.assertNotIn("buckaroo_paypal_order_id", session)

    def test_missing_session_order_id_raises_validation_error(self):
        tx = self._create_buckaroo_tx(reference="TX-PP-NO-ID", payment_method=self.paypal)
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with patch(_PAYPAL_PS) as MockPS, _patch_request({}):
            MockPS.return_value.create_payment.return_value = mock_builder
            with self.assertRaises(ValidationError):
                self.paypal._buckaroo_create_payment(tx, client)

    def test_non_paypal_method_delegates_to_base(self):
        tx = self._create_buckaroo_tx(reference="TX-IDEAL-PP-OFF")
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService",
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.ideal._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        param_keys = [call.args[0] for call in mock_builder.add_parameter.call_args_list]
        self.assertNotIn("payPalOrderId", param_keys)
        self.assertFalse(tx.buckaroo_official_is_paypal_express)


@tagged("post_install", "-at_install")
class TestPaypalPushAddress(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.paypal = cls.env.ref("payment_buckaroo_official.payment_method_paypal")
        cls.nl = cls.env.ref("base.nl")

    def _stub_partner(self, name="PayPal Customer"):
        return self.env["res.partner"].create({"name": name, "email": ""})

    def _express_tx(self, partner, sale_order=None):
        vals = {
            "provider_id": self.buckaroo.id,
            "payment_method_id": self.paypal.id,
            "reference": "TX-PP-PUSH",
            "amount": 50.0,
            "currency_id": self.currency_euro.id,
            "partner_id": partner.id,
            "operation": "online_redirect",
            "buckaroo_official_is_paypal_express": True,
        }
        if sale_order is not None:
            vals["sale_order_ids"] = [Command.link(sale_order.id)]
        return self.env["payment.transaction"].create(vals)

    def _full_push(self):
        return _paypal_push(
            payerFirstname="Ada",
            payerLastname="Lovelace",
            address_line_1="Keizersgracht 424",
            admin_area_2="Amsterdam",
            postal_code="1016 GC",
            payerCountry="NL",
            payerEmail="ada@example.com",
        )

    def _assert_payer_address(self, partner):
        partner.invalidate_recordset()
        self.assertEqual(partner.name, "Ada Lovelace")
        self.assertEqual(partner.street, "Keizersgracht 424")
        self.assertEqual(partner.city, "Amsterdam")
        self.assertEqual(partner.zip, "1016 GC")
        self.assertEqual(partner.country_id, self.nl)
        self.assertEqual(partner.email, "ada@example.com")

    def test_push_overwrites_stub_billing_partner(self):
        billing = self._stub_partner()
        tx = self._express_tx(billing)

        self.paypal._buckaroo_apply_push_metadata(tx, self._full_push())

        self._assert_payer_address(billing)

    def test_push_writes_distinct_shipping_partner_too(self):
        billing = self._stub_partner("PayPal Customer")
        shipping = self._stub_partner("Ship To")
        order = self.env["sale.order"].create(
            {
                "partner_id": billing.id,
                "partner_shipping_id": shipping.id,
            }
        )
        tx = self._express_tx(billing, sale_order=order)

        self.paypal._buckaroo_apply_push_metadata(tx, self._full_push())

        self._assert_payer_address(billing)
        self._assert_payer_address(shipping)

    def test_invalid_email_is_skipped_keeping_stub(self):
        billing = self._stub_partner()
        billing.email = "stub@placeholder.local"
        push = _paypal_push(
            payerFirstname="Ada",
            payerLastname="Lovelace",
            payerEmail="not-an-email",
        )

        self.paypal._buckaroo_apply_push_metadata(tx := self._express_tx(billing), push)

        billing.invalidate_recordset()
        self.assertEqual(billing.name, "Ada Lovelace")
        self.assertEqual(billing.email, "stub@placeholder.local")
        self.assertTrue(tx.buckaroo_official_is_paypal_express)

    def test_unresolved_country_left_untouched(self):
        billing = self._stub_partner()
        billing.country_id = self.nl
        push = _paypal_push(
            payerFirstname="Ada",
            payerLastname="Lovelace",
            payerCountry="ZZ",
        )

        self.paypal._buckaroo_apply_push_metadata(self._express_tx(billing), push)

        billing.invalidate_recordset()
        self.assertEqual(billing.country_id, self.nl)

    def test_non_express_paypal_tx_is_not_mutated(self):
        billing = self._stub_partner()
        tx = self._express_tx(billing)
        tx.buckaroo_official_is_paypal_express = False

        self.paypal._buckaroo_apply_push_metadata(tx, self._full_push())

        billing.invalidate_recordset()
        self.assertEqual(billing.name, "PayPal Customer")
        self.assertFalse(billing.street)
        self.assertFalse(billing.city)


@tagged("post_install", "-at_install")
class TestPaypalNoRedirectSettlement(BuckarooOfficialCommon):
    """The capture-on-pay settlement path: a PayPal Express ``pay`` returns
    no redirect URL (the buyer already approved in the sheet), so
    ``_buckaroo_handle_no_redirect_response`` settles the tx from the sync
    response and routes via ``/shop/payment/validate``."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.paypal = cls.env.ref("payment_buckaroo_official.payment_method_paypal")
        cls.buckaroo.payment_method_ids = [Command.link(cls.paypal.id)]
        cls.nl = cls.env.ref("base.nl")

    def _express_tx(self, reference="TX-PP-NR"):
        return self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.paypal.id,
                "reference": reference,
                "amount": 50.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.env["res.partner"]
                .create({"name": "PayPal Customer", "email": ""})
                .id,
                "operation": "online_redirect",
                "buckaroo_official_is_paypal_express": True,
            }
        )

    @staticmethod
    def _response_with_address(status_code):
        response = make_mock_sdk_response(status_code)
        addr = {
            "payerFirstname": "Ada",
            "payerLastname": "Lovelace",
            "address_line_1": "Keizersgracht 424",
            "admin_area_2": "Amsterdam",
            "postal_code": "1016 GC",
            "payerCountry": "NL",
            "payerEmail": "ada@example.com",
        }
        response.get_service_parameter.side_effect = lambda name: addr.get(name)
        return response

    def test_success_sets_done_writes_address_and_routes_to_validate(self):
        tx = self._express_tx()
        response = self._response_with_address(190)

        result = self.paypal._buckaroo_handle_no_redirect_response(tx, response)

        self.assertTrue(result["api_url"].endswith("/shop/payment/validate"))
        self.assertEqual(tx.provider_reference, response.key)
        self.assertEqual(tx.state, "done")
        partner = tx.partner_id
        partner.invalidate_recordset()
        self.assertEqual(partner.name, "Ada Lovelace")
        self.assertEqual(partner.street, "Keizersgracht 424")
        self.assertEqual(partner.city, "Amsterdam")
        self.assertEqual(partner.country_id, self.nl)
        self.assertEqual(partner.email, "ada@example.com")

    def test_pending_sets_pending_and_routes_to_validate(self):
        tx = self._express_tx("TX-PP-NR-PENDING")
        response = make_mock_sdk_response(791)

        result = self.paypal._buckaroo_handle_no_redirect_response(tx, response)

        self.assertTrue(result["api_url"].endswith("/shop/payment/validate"))
        self.assertEqual(tx.provider_reference, response.key)
        self.assertEqual(tx.state, "pending")

    def test_failure_returns_none_to_fall_through_to_error_path(self):
        tx = self._express_tx("TX-PP-NR-FAIL")
        response = make_mock_sdk_response(490)

        result = self.paypal._buckaroo_handle_no_redirect_response(tx, response)

        self.assertIsNone(result)
        self.assertNotEqual(tx.state, "done")

    def test_non_paypal_method_delegates_to_base(self):
        tx = self._create_buckaroo_tx(reference="TX-IDEAL-NR")
        result = self.ideal._buckaroo_handle_no_redirect_response(
            tx, make_mock_sdk_response(190)
        )
        self.assertIsNone(result)


@tagged("post_install", "-at_install")
class TestPaypalMerchantIdAndConfig(BuckarooOfficialCommon):
    """Mode-aware merchant id and configuration gating: the SDK environment
    (test vs live) is picked client-side from the provider state, so the
    merchant id must track the same state or the order can't be captured."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.paypal = cls.env.ref("payment_buckaroo_official.payment_method_paypal")
        cls.buckaroo.payment_method_ids = [Command.link(cls.paypal.id)]
        cls.paypal.buckaroo_official_paypal_merchant_id = "LIVE-MID"
        cls.paypal.buckaroo_official_paypal_sandbox_merchant_id = "SANDBOX-MID"

    def test_merchant_id_is_sandbox_in_test_mode(self):
        self.buckaroo.state = "test"
        self.assertEqual(
            self.paypal._buckaroo_paypal_merchant_id_for(self.buckaroo), "SANDBOX-MID"
        )

    def test_merchant_id_is_live_in_enabled_mode(self):
        self.buckaroo.state = "enabled"
        self.assertEqual(
            self.paypal._buckaroo_paypal_merchant_id_for(self.buckaroo), "LIVE-MID"
        )

    def test_configured_requires_matching_mode_id(self):
        self.buckaroo.state = "test"
        self.assertTrue(self.paypal._buckaroo_paypal_is_configured())
        self.paypal.buckaroo_official_paypal_sandbox_merchant_id = False
        self.assertFalse(self.paypal._buckaroo_paypal_is_configured())

    def test_configured_false_when_live_id_missing_in_production(self):
        self.buckaroo.state = "enabled"
        self.paypal.buckaroo_official_paypal_merchant_id = False
        self.assertFalse(self.paypal._buckaroo_paypal_is_configured())
