# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Unit tests for the Trustly payment method.

Trustly's Pay action requires ``customerFirstName``, ``customerLastName``,
``customerCountryCode`` and ``consumeremail``; without them the SDK raises
before the request leaves. These tests assert the params are added before
``.pay()`` and that the override stays scoped to ``buckaroo_trustly``.
SDK transport is mocked.
"""

from unittest.mock import MagicMock, patch

from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon, make_mock_sdk_builder

_MODULE = "odoo.addons.payment_buckaroo_official.models"


@tagged("post_install", "-at_install")
class TestTrustlyPaymentCreation(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.trustly = cls.env.ref("payment_buckaroo_official.payment_method_trustly")
        cls.ideal = cls.env.ref("payment_buckaroo_official.payment_method_ideal")
        cls.buckaroo.payment_method_ids = [
            Command.link(cls.trustly.id),
            Command.link(cls.ideal.id),
        ]
        cls.partner_se = cls.env["res.partner"].create(
            {
                "name": "Jan de Vries",
                "country_id": cls.env.ref("base.se").id,
                "email": "jan@example.se",
            }
        )

    def _create_tx(self, partner=None, method=None, reference="TRL-TX-001"):
        return self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": (method or self.trustly).id,
                "reference": reference,
                "amount": 100.0,
                "currency_id": self.currency_euro.id,
                "partner_id": (partner or self.partner_se).id,
                "operation": "online_redirect",
            }
        )

    def _create_payment(self, method, tx):
        """Run ``_buckaroo_create_payment`` with both the Trustly and the
        generic PaymentService patched, so the caller can tell which path ran."""
        mock_builder, mock_response = make_mock_sdk_builder()

        with (
            patch(f"{_MODULE}.payment_method_trustly.PaymentService") as MockOwn,
            patch(f"{_MODULE}.payment_method.PaymentService") as MockGeneric,
        ):
            MockOwn.return_value.create_payment.return_value = mock_builder
            MockGeneric.return_value.create_payment.return_value = mock_builder
            result = method._buckaroo_create_payment(tx, MagicMock())

        return mock_builder, mock_response, result, MockOwn, MockGeneric

    def test_trustly_adds_required_customer_parameters(self):
        tx = self._create_tx()

        mock_builder, mock_response, result, _own, _generic = self._create_payment(self.trustly, tx)

        params = {call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list}
        self.assertEqual(params["consumeremail"], "jan@example.se")
        self.assertEqual(params["customerFirstName"], "Jan")
        self.assertEqual(params["customerLastName"], "de Vries")
        self.assertEqual(params["customerCountryCode"], "SE")
        mock_builder.pay.assert_called_once()
        self.assertEqual(result, mock_response)

    def test_trustly_mononym_falls_back_to_first_name(self):
        """A single-word partner name leaves last_name empty; Buckaroo requires
        both parts, so the first name is reused rather than sent blank."""
        partner = self.env["res.partner"].create(
            {
                "name": "Prince",
                "country_id": self.env.ref("base.se").id,
                "email": "prince@example.se",
            }
        )
        tx = self._create_tx(partner=partner, reference="TRL-TX-MONONYM")

        mock_builder, _response, _result, _own, _generic = self._create_payment(self.trustly, tx)

        params = {call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list}
        self.assertEqual(params["customerFirstName"], "Prince")
        self.assertEqual(params["customerLastName"], "Prince")

    def test_other_method_delegates_to_generic_path(self):
        tx = self._create_tx(method=self.ideal, reference="TRL-TX-IDEAL")

        mock_builder, _response, _result, MockOwn, MockGeneric = self._create_payment(
            self.ideal, tx
        )

        MockOwn.return_value.create_payment.assert_not_called()
        MockGeneric.return_value.create_payment.assert_called_once()
        mock_builder.add_parameter.assert_not_called()
