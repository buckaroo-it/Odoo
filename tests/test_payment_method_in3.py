# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Tests for the In3 payment method.

Covers payment creation dispatch, refund flow, country/currency restrictions,
shop-controller birthdate validation, partner persistence, and the
dict→In3 API formatters (B2C + B2B). SDK transport is mocked throughout.
"""

from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import BaseCase, tagged

from .common import (
    BuckarooOfficialCommon,
    REFUND_STATUS_CASES,
    make_mock_sdk_builder,
    make_mock_sdk_response,
    parsed_from_form,
)
from ..helpers.customer import get_customer_data
from ..models.payment_method_in3 import PaymentMethodIn3 as In3PaymentMethod
from .test_helpers import make_partner


@tagged("post_install", "-at_install")
class TestIn3CreatePaymentDispatch(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.in3 = cls.env.ref("payment_buckaroo_official.payment_method_in3")
        cls.buckaroo.payment_method_ids = [Command.link(cls.in3.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {
                "name": "Jan",
                "street": "Keizersgracht 424",
                "zip": "1016 GC",
                "city": "Amsterdam",
                "country_id": cls.env.ref("base.nl").id,
                "email": "jan@example.nl",
                "phone": "+31612345678",
            }
        )

    def test_non_in3_skips_add_parameter(self):
        tx = self._create_buckaroo_tx(reference="TX-NB-001", payment_method=self.ideal)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        mock_builder.add_parameter.assert_not_called()
        self.assertEqual(result, mock_response)

    def test_in3_adds_article_and_customer_parameters(self):
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.in3.id,
                "reference": "TX-IN3-001",
                "amount": 75.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )
        product = self.env["product.product"].create(
            {"name": "Widget", "default_code": "WIDGET-01", "list_price": 75.0}
        )
        order = self.env["sale.order"].create({"partner_id": self.partner_nl.id})
        self.env["sale.order.line"].create(
            {
                "order_id": order.id,
                "product_id": product.id,
                "product_uom_qty": 1,
                "price_unit": 75.0,
            }
        )
        tx.sale_order_ids = [Command.link(order.id)]

        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_in3.PaymentService"
            ) as MockPS,
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_in3.resolve_birthdate",
                return_value="1990-01-01",
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.in3._buckaroo_create_payment(tx, client)

        param_names = [call[0][0] for call in mock_builder.add_parameter.call_args_list]
        self.assertIn("article", param_names)
        self.assertIn("billingCustomer", param_names)
        self.assertIn("shippingCustomer", param_names)
        mock_builder.pay.assert_called_once()
        self.assertEqual(result, mock_response)

        billing_call = next(
            call for call in mock_builder.add_parameter.call_args_list
            if call[0][0] == "billingCustomer"
        )
        billing = billing_call[0][1][0]
        self.assertEqual(billing["BirthDate"], "1990-01-01")
        self.assertEqual(billing["Category"], "B2C")
        self.assertEqual(billing["CountryCode"], "NL")


@tagged("post_install", "-at_install")
class TestIn3RefundFlow(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.in3 = cls.env.ref("payment_buckaroo_official.payment_method_in3")
        cls.buckaroo.payment_method_ids = [Command.link(cls.in3.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {"name": "Jan", "country_id": cls.env.ref("base.nl").id}
        )

    def _create_done_tx(self, reference="IN3-DONE", amount=100.0):
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.in3.id,
                "reference": reference,
                "amount": amount,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )
        tx._apply_updates(
            parsed_from_form(
                {
                    "brq_invoicenumber": reference,
                    "brq_amount": str(amount),
                    "brq_currency": "EUR",
                    "brq_statuscode": "190",
                    "brq_transactions": "IN3_DONE_KEY",
                }
            )
        )
        self.assertEqual(tx.state, "done")
        return tx

    def _patch_refund(self, response):
        PaymentMethod = type(self.env["payment.method"])
        return patch.object(PaymentMethod, "_buckaroo_create_refund", return_value=response)

    def test_refund_status_matrix(self):
        for status_code, expected_state in REFUND_STATUS_CASES:
            with self.subTest(status_code=status_code, expected_state=expected_state):
                tx = self._create_done_tx(reference="IN3-REFUND-%s" % status_code)
                response = make_mock_sdk_response(status_code)
                response.key = "IN3_REFUND_%s" % status_code
                with self._patch_refund(response):
                    refund_tx = tx._refund()
                self.assertEqual(refund_tx.state, expected_state)


@tagged("post_install", "-at_install")
class TestIn3CountryRestriction(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.in3 = cls.env.ref("payment_buckaroo_official.payment_method_in3")
        cls.buckaroo.payment_method_ids = [Command.link(cls.in3.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {"name": "NL", "country_id": cls.env.ref("base.nl").id}
        )
        cls.partner_de = cls.env["res.partner"].create(
            {"name": "DE", "country_id": cls.env.ref("base.de").id}
        )

    def _visible(self, partner, currency=None):
        methods = self.env["payment.method"]._get_compatible_payment_methods(
            self.buckaroo.ids,
            partner.id,
            currency_id=(currency or self.currency_euro).id,
            amount=100.0,
        )
        return self.in3 in methods

    def test_nl_visible(self):
        self.assertTrue(self._visible(self.partner_nl))

    def test_de_hidden(self):
        self.assertFalse(self._visible(self.partner_de))

    def test_non_eur_currency_hidden(self):
        self.assertFalse(self._visible(self.partner_nl, currency=self.env.ref("base.USD")))


@tagged("post_install", "-at_install")
class TestIn3ShopPaymentControllerValidations(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.in3 = cls.env.ref("payment_buckaroo_official.payment_method_in3")
        cls.buckaroo.payment_method_ids = [Command.link(cls.in3.id)]

    def _invoke(self, **kwargs):
        import odoo.http
        from odoo.addons.payment_buckaroo_official.controllers.in3 import In3PaymentPortal

        controller = In3PaymentPortal()
        mock_request = MagicMock()
        mock_request.env = self.env
        mock_request.session = {}
        with (
            patch.object(odoo.http, "request", mock_request),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.in3.request",
                new=mock_request,
            ),
        ):
            return controller.shop_payment_transaction(
                order_id=1,
                access_token="ignored",
                payment_method_id=self.in3.id,
                **kwargs,
            )

    def test_missing_birthdate_raises(self):
        with self.assertRaises(ValidationError) as ctx:
            self._invoke()
        self.assertIn("date of birth", str(ctx.exception))

    def test_invalid_birthdate_format_raises(self):
        with self.assertRaises(ValidationError) as ctx:
            self._invoke(in3_birthdate="not-a-date")
        self.assertIn("Invalid date of birth", str(ctx.exception))

    def test_under_18_raises(self):
        with self.assertRaises(ValidationError) as ctx:
            self._invoke(in3_birthdate="2020-01-01")
        self.assertIn("18", str(ctx.exception))

    def test_valid_birthdate_persists_on_logged_in_user_partner(self):
        from datetime import date

        partner = self.env.user.partner_id
        partner.buckaroo_in3_birthdate = False
        with patch(
            "odoo.addons.website_sale.controllers.payment.PaymentPortal.shop_payment_transaction",
            return_value="SUPER_OK",
        ):
            self._invoke(in3_birthdate="1990-05-15")
        self.assertEqual(partner.buckaroo_in3_birthdate, date(1990, 5, 15))


class TestFormatIn3Articles(BaseCase):
    def test_maps_generic_dict_to_in3_pascalcase(self):
        generic = [
            {
                "identifier": "SKU-1",
                "description": "Widget",
                "quantity": 2.0,
                "unit_price_incl": 12.10,
                "unit_price_excl": 10.0,
                "vat_percentage": 21.0,
                "vat_amount": 4.20,
                "type": "product",
            }
        ]
        result = In3PaymentMethod._format_in3_articles(generic)
        self.assertEqual(len(result), 1)
        a = result[0]
        self.assertEqual(a["Identifier"], "SKU-1")
        self.assertEqual(a["Description"], "Widget")
        self.assertEqual(a["Quantity"], "2")
        self.assertAlmostEqual(a["GrossUnitPrice"], 12.10, places=2)
        self.assertAlmostEqual(a["VatPercentage"], 21.0, places=2)


class TestFormatIn3Customer(BaseCase):
    def test_b2c_base_customer_maps_to_pascalcase(self):
        data = get_customer_data(
            make_partner(
                name="Jan de Vries",
                street="Keizersgracht 424",
                zip_code="1016 GC",
                city="Amsterdam",
                country_code="NL",
                email="jan@example.com",
                phone="+31612345678",
            )
        )
        result = In3PaymentMethod._format_in3_customer(data)
        self.assertEqual(result["Category"], "B2C")
        self.assertEqual(result["FirstName"], "Jan")
        self.assertEqual(result["LastName"], "de Vries")
        self.assertEqual(result["Initials"], "J.D.V.")
        self.assertEqual(result["Street"], "Keizersgracht")
        self.assertEqual(result["StreetNumber"], "424")
        self.assertEqual(result["PostalCode"], "1016 GC")
        self.assertEqual(result["City"], "Amsterdam")
        self.assertEqual(result["CountryCode"], "NL")
        # Slot-specific fields are added by the caller, not here.
        self.assertNotIn("Email", result)
        self.assertNotIn("Phone", result)
        self.assertNotIn("CustomerNumber", result)
        self.assertNotIn("BirthDate", result)
        self.assertNotIn("CareOf", result)
        self.assertNotIn("CompanyName", result)

    def test_b2b_customer_includes_company_and_coc(self):
        data = get_customer_data(
            make_partner(
                is_company=True,
                commercial_company_name="Acme BV",
                company_registry="12345678",
            )
        )
        result = In3PaymentMethod._format_in3_customer(data)
        self.assertEqual(result["Category"], "B2B")
        self.assertEqual(result["CompanyName"], "Acme BV")
        self.assertEqual(result["CocNumber"], "12345678")
