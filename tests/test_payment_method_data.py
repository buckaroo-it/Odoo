# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo.tests import tagged

from .common import BuckarooOfficialCommon
from ..utils import const


ALL_XML_IDS = [
    f'payment_buckaroo_official.payment_method_{code}'
    for code in const.DEFAULT_PAYMENT_METHOD_CODES
]


# Full currency set shared by PayPal and Credit Card (see data/payment_method_data.xml).
_MULTI_CURRENCY = {
    'USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY', 'CHF', 'HKD', 'SGD', 'SEK',
    'DKK', 'NOK', 'NZD', 'THB', 'HUF', 'CZK', 'ILS', 'MXN', 'BRL', 'MYR',
    'PHP', 'TWD',
}


# (code, expected_currencies, expected_countries) — empty set = no restriction.
METHOD_DATA = [
    ('ideal',       {'EUR'},                              set()),
    ('bancontact',  {'EUR'},                              {'BE'}),
    ('wero',        {'EUR'},                              set()),
    ('eps',         {'EUR'},                              {'AT'}),
    ('belfius',     {'EUR'},                              {'BE'}),
    ('kbc',         {'EUR'},                              {'BE'}),
    ('alipay',      {'EUR'},                              set()),
    ('wechatpay',   {'EUR'},                              set()),
    ('payconiq',    {'EUR'},                              {'BE'}),
    ('swish',       {'SEK'},                              {'SE'}),
    ('bizum',       {'EUR'},                              {'ES'}),
    ('mbway',       {'EUR'},                              {'PT'}),
    ('multibanco',  {'EUR'},                              {'PT'}),
    ('knaken',      {'EUR'},                              set()),
    ('paypal',      _MULTI_CURRENCY,                      set()),
    ('trustly',     {'EUR', 'SEK', 'NOK', 'DKK', 'GBP'},  set()),
    ('przelewy24',  {'EUR', 'PLN'},                       {'PL'}),
    ('blik',        {'PLN'},                              {'PL'}),
    ('twint',       {'CHF'},                              {'CH'}),
    ('billink',     {'EUR'},                              {'NL', 'BE'}),
    ('creditcard',  _MULTI_CURRENCY,                      set()),
]


@tagged('post_install', '-at_install')
class TestBuckarooOfficialPaymentMethodData(BuckarooOfficialCommon):
    """Verify that all 21 payment.method XML records are correctly installed.

    Checks record existence, support_refund, and per-method code/currency/country
    restrictions as defined in ``data/payment_method_data.xml``.
    """

    def test_all_21_payment_method_records_exist(self):
        for xml_id in ALL_XML_IDS:
            with self.subTest(xml_id=xml_id):
                record = self.env.ref(xml_id)
                self.assertTrue(record.exists(), "Record %s not found" % xml_id)

    def test_all_methods_support_refund_partial(self):
        for xml_id in ALL_XML_IDS:
            with self.subTest(xml_id=xml_id):
                record = self.env.ref(xml_id)
                self.assertEqual(
                    record.support_refund, 'partial',
                    "Expected support_refund='partial' for %s, got '%s'" % (
                        xml_id, record.support_refund,
                    ),
                )

    def test_payment_method_xml_data(self):
        for code, currencies, countries in METHOD_DATA:
            with self.subTest(code=code):
                method = self.env.ref(f'payment_buckaroo_official.payment_method_{code}')
                self.assertEqual(method.code, code)
                self.assertEqual(
                    set(method.supported_country_ids.mapped('code')), countries,
                )
                self.assertEqual(
                    set(method.supported_currency_ids.mapped('name')), currencies,
                )
