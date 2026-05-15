# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon
from ..utils import const


ALL_XML_IDS = [
    f"payment_buckaroo_official.payment_method_{code}"
    for code in const.DEFAULT_PAYMENT_METHOD_CODES
]


# Full currency set shared by PayPal and Credit Card (see data/payment_method_data.xml).
_MULTI_CURRENCY = {
    "USD",
    "EUR",
    "GBP",
    "CAD",
    "AUD",
    "JPY",
    "CHF",
    "HKD",
    "SGD",
    "SEK",
    "DKK",
    "NOK",
    "NZD",
    "THB",
    "HUF",
    "CZK",
    "ILS",
    "MXN",
    "BRL",
    "MYR",
    "PHP",
    "TWD",
}


# (code, expected_currencies, expected_countries) — empty set = no restriction.
# Country lists are intentionally empty for *most* methods: Buckaroo gates
# country eligibility per method on its side, so Odoo only enforces currency.
# BNPL methods (Klarna, Riverty) are the exception: they pin a country list
# explicitly to keep BNPL only visible to shoppers from supported markets.
METHOD_DATA = [
    ("ideal", {"EUR"}, set()),
    ("bancontact", {"EUR"}, set()),
    ("wero", {"EUR"}, set()),
    ("eps", {"EUR"}, set()),
    ("belfius", {"EUR"}, set()),
    ("kbc", {"EUR"}, set()),
    ("alipay", {"EUR"}, set()),
    ("wechatpay", {"EUR"}, set()),
    ("payconiq", {"EUR"}, set()),
    ("swish", {"SEK"}, set()),
    ("bizum", {"EUR"}, set()),
    ("mbway", {"EUR"}, set()),
    ("multibanco", {"EUR"}, set()),
    ("knaken", {"EUR"}, set()),
    ("paypal", _MULTI_CURRENCY, set()),
    ("trustly", {"EUR", "SEK", "NOK", "DKK", "GBP"}, set()),
    ("przelewy24", {"EUR", "PLN"}, set()),
    ("blik", {"PLN"}, set()),
    ("twint", {"CHF"}, set()),
    ("googlepay", {"EUR"}, set()),
    ("applepay", {"EUR"}, set()),
    ("billink", {"EUR"}, set()),
    ("in3", {"EUR"}, {"NL"}),
    ("bank_transfer", {"EUR"}, set()),
    ("creditcard", _MULTI_CURRENCY, set()),
    (
        "klarna",
        {"EUR", "CHF", "DKK", "NOK", "SEK", "PLN", "GBP"},
        # ``base.uk`` resolves to country code 'GB' in Odoo (United
        # Kingdom of Great Britain).
        {
            "NL",
            "BE",
            "DE",
            "AT",
            "FI",
            "FR",
            "ES",
            "IT",
            "PT",
            "IE",
            "CH",
            "DK",
            "NO",
            "SE",
            "PL",
            "GB",
        },
    ),
    ("riverty", {"EUR"}, {"NL", "BE", "DE", "AT", "FI"}),
]


@tagged("post_install", "-at_install")
class TestBuckarooOfficialPaymentMethodData(BuckarooOfficialCommon):
    """Verify that all 22 payment.method XML records are correctly installed.

    Checks record existence, support_refund, and per-method code/currency/country
    restrictions as defined in ``data/payment_method_data.xml``.
    """

    def test_all_payment_method_records_exist(self):
        for xml_id in ALL_XML_IDS:
            with self.subTest(xml_id=xml_id):
                record = self.env.ref(xml_id)
                self.assertTrue(record.exists(), "Record %s not found" % xml_id)

    def test_methods_support_refund(self):
        # Riverty refund is full-only because Odoo's amount-based refund
        # flow can't supply the article-level breakdown Riverty requires.
        full_only_codes = {"riverty"}
        for xml_id in ALL_XML_IDS:
            with self.subTest(xml_id=xml_id):
                record = self.env.ref(xml_id)
                expected = "full_only" if record.code in full_only_codes else "partial"
                self.assertEqual(
                    record.support_refund,
                    expected,
                    "Expected support_refund='%s' for %s, got '%s'"
                    % (
                        expected,
                        xml_id,
                        record.support_refund,
                    ),
                )

    def test_payment_method_xml_data(self):
        for code, currencies, countries in METHOD_DATA:
            with self.subTest(code=code):
                method = self.env.ref(f"payment_buckaroo_official.payment_method_{code}")
                self.assertEqual(method.code, code)
                self.assertEqual(
                    set(method.supported_country_ids.mapped("code")),
                    countries,
                )
                self.assertEqual(
                    set(method.supported_currency_ids.mapped("name")),
                    currencies,
                )

    def test_methods_available_for_nl_partner(self):
        pm_ids = [
            self.env.ref(f"payment_buckaroo_official.payment_method_{code}").id
            for code, _c, _co in METHOD_DATA
        ]
        self.buckaroo.payment_method_ids = [Command.set(pm_ids)]
        self.env.ref(
            "payment_buckaroo_official.payment_method_googlepay"
        ).buckaroo_official_googlepay_merchant_guid = "test_merchant_guid"
        self.env.flush_all()

        nl_partner = self.env["res.partner"].create(
            {
                "name": "Test NL Partner",
                "country_id": self.env.ref("base.nl").id,
            }
        )

        for code, currencies, _countries in METHOD_DATA:
            with self.subTest(code=code):
                pm = self.env.ref(f"payment_buckaroo_official.payment_method_{code}")
                currency = self.env.ref(f"base.{next(iter(currencies))}")
                methods = self.env["payment.method"]._get_compatible_payment_methods(
                    self.buckaroo.ids,
                    nl_partner.id,
                    currency_id=currency.id,
                )
                self.assertIn(pm, methods)

    def test_mbway_excluded_for_non_eur_currency(self):
        mbway = self.env.ref("payment_buckaroo_official.payment_method_mbway")
        self.buckaroo.payment_method_ids = [Command.link(mbway.id)]

        nl_partner = self.env["res.partner"].create(
            {
                "name": "Test NL Partner",
                "country_id": self.env.ref("base.nl").id,
            }
        )

        methods = self.env["payment.method"]._get_compatible_payment_methods(
            self.buckaroo.ids,
            nl_partner.id,
            currency_id=self.env.ref("base.USD").id,
        )
        self.assertNotIn(mbway, methods)
