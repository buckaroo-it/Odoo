# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo.fields import Command
from odoo.tests import tagged
from odoo.tools import convert_file

from .common import BuckarooOfficialCommon
from ..utils import const


ALL_XML_IDS = [
    f"payment_buckaroo_official.payment_method_{code.removeprefix('buckaroo_')}"
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
    ("paypermail", {"EUR"}, set()),
]


@tagged("post_install", "-at_install")
class TestBuckarooOfficialPaymentMethodData(BuckarooOfficialCommon):
    """Verify that all top-level payment methods are correctly installed.

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
        full_only_codes = {"buckaroo_riverty"}
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
                self.assertEqual(method.code, f"buckaroo_{code}")
                self.assertEqual(
                    set(method.supported_country_ids.mapped("code")),
                    countries,
                )
                self.assertEqual(
                    set(method.supported_currency_ids.mapped("name")),
                    currencies,
                )

        giftcard = self.env.ref("payment_buckaroo_official.payment_method_giftcard")
        self.assertFalse(giftcard.supported_country_ids)
        self.assertEqual(
            set(giftcard.supported_currency_ids.mapped("name")),
            {"EUR"},
        )

    def _reload_payment_method_data(self):
        for path in (
            "data/payment_method_data.xml",
            "data/payment_provider_data.xml",
        ):
            convert_file(
                self.env,
                "payment_buckaroo_official",
                path,
                {},
                mode="update",
            )

    def test_module_upgrade_preserves_merchant_fields(self):
        billink = self.env.ref("payment_buckaroo_official.payment_method_billink")
        custom_image = self.env.ref("payment_buckaroo_official.payment_method_ideal").image
        self.assertNotEqual(custom_image, billink.image)
        billink.write(
            {
                "name": "Custom Billink",
                "active": False,
                "sequence": 5,
                "image": custom_image,
                "supported_country_ids": [Command.set([self.env.ref("base.de").id])],
                "supported_currency_ids": [Command.set([self.env.ref("base.USD").id])],
                "buckaroo_official_min_amount": "10.00",
                "buckaroo_official_max_amount": "20.00",
            }
        )
        self.buckaroo.payment_method_ids = [Command.unlink(billink.id)]

        self._reload_payment_method_data()

        self.assertEqual(billink.name, "Custom Billink")
        self.assertFalse(billink.active)
        self.assertEqual(billink.sequence, 5)
        self.assertEqual(billink.image, custom_image)
        self.assertEqual(set(billink.supported_country_ids.mapped("code")), {"DE"})
        self.assertEqual(set(billink.supported_currency_ids.mapped("name")), {"USD"})
        self.assertEqual(billink.buckaroo_official_min_amount, "10.00")
        self.assertEqual(billink.buckaroo_official_max_amount, "20.00")
        self.assertNotIn(
            billink,
            self.buckaroo.with_context(active_test=False).payment_method_ids,
        )

    def test_module_upgrade_recreates_missing_method(self):
        xml_id = "payment_buckaroo_official.payment_method_brand_nexi"
        method = self.env.ref(xml_id)
        self.env["ir.model.data"].search(
            [
                ("module", "=", "payment_buckaroo_official"),
                ("name", "=", "payment_method_brand_nexi"),
            ]
        ).unlink()
        method.unlink()

        self._reload_payment_method_data()

        recreated = self.env.ref(xml_id)
        self.assertEqual(recreated.name, "Nexi")
        self.assertEqual(recreated.code, "buckaroo_nexi")
        self.assertTrue(recreated.active)
        self.assertEqual(recreated.sequence, 250)
        self.assertTrue(recreated.image)

    def test_methods_available_for_nl_partner(self):
        pm_ids = [
            self.env.ref(f"payment_buckaroo_official.payment_method_{code}").id
            for code, _c, _co in METHOD_DATA
        ]
        self.buckaroo.payment_method_ids = [Command.set(pm_ids)]
        self.env.ref(
            "payment_buckaroo_official.payment_method_googlepay"
        ).buckaroo_official_googlepay_merchant_guid = "test_merchant_guid"
        self.env.ref(
            "payment_buckaroo_official.payment_method_applepay"
        ).buckaroo_official_applepay_merchant_guid = "test_merchant_guid"
        # PayPal shows on checkout only when configured for the provider's mode
        # and flagged on. The provider runs in test mode (reads the sandbox id),
        # and show_on_checkout can be off on upgraded records, so set both.
        paypal = self.env.ref("payment_buckaroo_official.payment_method_paypal")
        paypal.buckaroo_official_paypal_merchant_id = "test_merchant_id"
        paypal.buckaroo_official_paypal_sandbox_merchant_id = "test_sandbox_merchant_id"
        paypal.buckaroo_official_paypal_show_on_checkout = True
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
