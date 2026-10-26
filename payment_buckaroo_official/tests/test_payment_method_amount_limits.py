# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon


# Every default Buckaroo method except 'creditcard' and 'billink' (both have
# dedicated subclasses with special handling and their own test suites).
METHODS_FIXTURE = [
    "ideal",
    "bancontact",
    "wero",
    "eps",
    "belfius",
    "kbc",
    "alipay",
    "wechatpay",
    "payconiq",
    "swish",
    "bizum",
    "mbway",
    "multibanco",
    "trustly",
    "przelewy24",
    "blik",
    "twint",
]


@tagged("post_install", "-at_install")
class TestBuckarooOfficialPaymentMethodAmountLimits(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Enable every method in the fixture and strip its country/currency
        # restrictions so only the amount-limit logic can exclude them during
        # the tests.
        for code in METHODS_FIXTURE:
            pm = cls._pm(f"buckaroo_{code}")
            pm.write(
                {
                    "active": True,
                    "supported_country_ids": [Command.clear()],
                    "supported_currency_ids": [Command.clear()],
                }
            )

    def test_amount_limit_excludes_below_min(self):
        for code in METHODS_FIXTURE:
            with self.subTest(code=code):
                pm = self._pm(f"buckaroo_{code}")
                pm.write(
                    {
                        "buckaroo_official_min_amount": "100.00",
                        "buckaroo_official_max_amount": "",
                    }
                )
                methods = self.buckaroo._find_available_payment_methods(
                    self.partner.id,
                    currency_id=self.currency_euro.id,
                    amount=50.00,
                )
                self.assertNotIn(pm, methods)

    def test_amount_limit_excludes_above_max(self):
        for code in METHODS_FIXTURE:
            with self.subTest(code=code):
                pm = self._pm(f"buckaroo_{code}")
                pm.write(
                    {
                        "buckaroo_official_min_amount": "",
                        "buckaroo_official_max_amount": "25.00",
                    }
                )
                methods = self.buckaroo._find_available_payment_methods(
                    self.partner.id,
                    currency_id=self.currency_euro.id,
                    amount=50.00,
                )
                self.assertNotIn(pm, methods)

    def test_amount_limit_includes_within_range(self):
        for code in METHODS_FIXTURE:
            with self.subTest(code=code):
                pm = self._pm(f"buckaroo_{code}")
                pm.write(
                    {
                        "buckaroo_official_min_amount": "10.00",
                        "buckaroo_official_max_amount": "100.00",
                    }
                )
                methods = self.buckaroo._find_available_payment_methods(
                    self.partner.id,
                    currency_id=self.currency_euro.id,
                    amount=50.00,
                )
                self.assertIn(pm, methods)

    def test_negative_min_amount_raises(self):
        with self.assertRaises(ValidationError):
            self.ideal.write({"buckaroo_official_min_amount": "-1"})

    def test_negative_max_amount_raises(self):
        with self.assertRaises(ValidationError):
            self.ideal.write({"buckaroo_official_max_amount": "-1"})

    def test_max_below_min_raises(self):
        with self.assertRaises(ValidationError):
            self.ideal.write(
                {
                    "buckaroo_official_min_amount": "100",
                    "buckaroo_official_max_amount": "50",
                }
            )
