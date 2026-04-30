# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""End-to-end + unit tests for the Billink payment method.

Covers payment creation, webhook state transitions (form + JSON), refund
lifecycle, amount/country gating, shop-controller validation, billink-only
dispatch on payment.method, birthdate session handling, and the dict→Billink
API formatters. SDK transport is mocked throughout.
"""

from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import BaseCase, tagged

from .common import (
    BuckarooOfficialCommon,
    make_mock_sdk_builder,
    make_mock_sdk_response,
    parsed_from_form,
    parsed_from_json,
)
from ..helpers.articles import get_order_articles
from ..helpers.customer import get_customer_data
from ..models.payment_method_billink import PaymentMethodBillink as BillinkPaymentMethod
from .test_helpers import make_partner


def _make_mock_order_line(
    product_code='SKU-001',
    product_id_val=1,
    name='Product',
    qty=1.0,
    price_total=12.10,
    price_subtotal=10.0,
    tax_amount=21.0,
    display_type=False,
):
    line = MagicMock()
    line.display_type = display_type
    line.is_delivery = False

    product = MagicMock()
    product.default_code = product_code
    product.id = product_id_val
    product.display_name = name
    product.barcode = None
    line.product_id = product
    line.name = name
    line.product_uom_qty = qty
    line.price_total = price_total
    line.price_subtotal = price_subtotal
    line.price_tax = round(price_total - price_subtotal, 2)

    tax = MagicMock()
    tax.amount_type = 'percent'
    tax.amount = tax_amount
    line.tax_ids = [tax] if tax_amount else []
    return line


@tagged('post_install', '-at_install')
class TestBillinkPaymentCreation(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.billink = cls.env.ref('payment_buckaroo_official.payment_method_billink')
        cls.buckaroo.payment_method_ids = [Command.link(cls.billink.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan de Vries',
            'country_id': cls.env.ref('base.nl').id,
            'email': 'jan@example.nl',
        })

    def _sdk_response(self, redirect_url='https://testcheckout.buckaroo.nl/pay/BL',
                      key='BL_KEY'):
        response = MagicMock()
        response.get_redirect_url.return_value = redirect_url
        response.key = key
        response.status_code = 200
        response.redirect_url = redirect_url
        response.required_action = None
        response.buckaroo_status_message = 'Success'
        response._raw_data = {}
        return response

    def _create_tx(self, reference='BILLINK-TX-001', amount=100.0):
        return self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.billink.id,
            'reference': reference,
            'amount': amount,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner_nl.id,
            'operation': 'online_redirect',
        })

    def test_payment_creation_returns_redirect_and_stores_reference(self):
        response = self._sdk_response(redirect_url='https://bl/pay/X', key='STORED_KEY')
        tx = self._create_tx()
        PaymentMethod = type(tx.payment_method_id)

        with patch.object(PaymentMethod, '_buckaroo_create_payment', return_value=response), \
             patch.object(PaymentMethod, '_buckaroo_extract_redirect_url',
                          return_value='https://bl/pay/X'):
            result = tx._get_specific_processing_values({})

        self.assertEqual(result['api_url'], 'https://bl/pay/X')
        self.assertEqual(tx.provider_reference, 'STORED_KEY')

    def test_payment_creation_missing_redirect_raises(self):
        response = self._sdk_response(redirect_url=None)
        tx = self._create_tx()
        PaymentMethod = type(tx.payment_method_id)

        with patch.object(PaymentMethod, '_buckaroo_create_payment', return_value=response), \
             patch.object(PaymentMethod, '_buckaroo_extract_redirect_url', return_value=None):
            with self.assertRaises(ValidationError):
                tx._get_specific_processing_values({})


@tagged('post_install', '-at_install')
class TestBillinkWebhookCallbacks(BuckarooOfficialCommon):

    CALLBACK_DONE_CODES = [190]
    CALLBACK_PENDING_CODES = [790, 791, 792, 793]
    CALLBACK_CANCEL_CODES = [890, 891]
    CALLBACK_ERROR_CODES = [490, 690]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.billink = cls.env.ref('payment_buckaroo_official.payment_method_billink')
        cls.buckaroo.payment_method_ids = [Command.link(cls.billink.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan de Vries',
            'country_id': cls.env.ref('base.nl').id,
        })

    def _create_tx(self, reference, amount=100.0):
        return self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.billink.id,
            'reference': reference,
            'amount': amount,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner_nl.id,
            'operation': 'online_redirect',
        })

    def _form_callback(self, reference, status_code, txn_key='BILLINK_TXN_001'):
        return parsed_from_form({
            'brq_invoicenumber': reference,
            'brq_amount': '100.00',
            'brq_currency': 'EUR',
            'brq_statuscode': str(status_code),
            'brq_transactions': txn_key,
        })

    def _json_callback(self, reference, status_code, txn_key='BILLINK_TXN_001'):
        return parsed_from_json({
            'Transaction': {
                'Invoice': reference,
                'AmountDebit': 100.0,
                'Currency': 'EUR',
                'Status': {'Code': {'Code': status_code, 'Description': 'Test'}},
                'Key': txn_key,
            },
        })

    def _assert_status_maps_to_state(self, codes, expected_state):
        for code in codes:
            for fmt in ('form', 'json'):
                with self.subTest(code=code, handler=fmt):
                    ref = 'BILLINK-WH-%s-%s' % (fmt.upper(), code)
                    tx = self._create_tx(ref)
                    builder = self._form_callback if fmt == 'form' else self._json_callback
                    parsed = builder(ref, code)
                    self.env['payment.transaction'].sudo()._process('buckaroo_official', parsed)
                    self.assertEqual(tx.state, expected_state)

    def test_done_status_maps_to_done(self):
        self._assert_status_maps_to_state(self.CALLBACK_DONE_CODES, 'done')
        # Spot-check provider_reference storage on the canonical success code.
        tx = self._create_tx('BILLINK-WH-REF-190')
        parsed = self._form_callback('BILLINK-WH-REF-190', 190)
        self.env['payment.transaction'].sudo()._process('buckaroo_official', parsed)
        self.assertEqual(tx.provider_reference, 'BILLINK_TXN_001')

    def test_pending_status_maps_to_pending(self):
        self._assert_status_maps_to_state(self.CALLBACK_PENDING_CODES, 'pending')

    def test_cancel_status_maps_to_cancel(self):
        self._assert_status_maps_to_state(self.CALLBACK_CANCEL_CODES, 'cancel')

    def test_error_status_maps_to_error(self):
        self._assert_status_maps_to_state(self.CALLBACK_ERROR_CODES, 'error')

    def test_e2e_creation_to_success_callback_sets_done(self):
        redirect_url = 'https://testcheckout.buckaroo.nl/pay/E2E'
        response = MagicMock()
        response.get_redirect_url.return_value = redirect_url
        response.key = 'E2E_KEY'
        response.status_code = 200
        response.redirect_url = redirect_url
        response.required_action = None
        response.buckaroo_status_message = 'Success'
        response._raw_data = {}

        tx = self._create_tx('BILLINK-E2E-001')
        PaymentMethod = type(tx.payment_method_id)
        with patch.object(PaymentMethod, '_buckaroo_create_payment', return_value=response), \
             patch.object(PaymentMethod, '_buckaroo_extract_redirect_url', return_value=redirect_url):
            result = tx._get_specific_processing_values({})

        self.assertEqual(result['api_url'], redirect_url)
        self.assertEqual(tx.provider_reference, 'E2E_KEY')

        callback = self._form_callback('BILLINK-E2E-001', 190, txn_key='E2E_KEY')
        self.env['payment.transaction'].sudo()._process('buckaroo_official', callback)
        self.assertEqual(tx.state, 'done')


@tagged('post_install', '-at_install')
class TestBillinkRefundFlow(BuckarooOfficialCommon):

    REFUND_STATUS_CASES = [
        (190, 'done'),
        (790, 'pending'),
        (890, 'cancel'),
        (490, 'error'),
    ]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.billink = cls.env.ref('payment_buckaroo_official.payment_method_billink')
        cls.buckaroo.payment_method_ids = [Command.link(cls.billink.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan de Vries',
            'country_id': cls.env.ref('base.nl').id,
        })

    def _create_done_tx(self, reference='BL-DONE', amount=100.0):
        tx = self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.billink.id,
            'reference': reference,
            'amount': amount,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner_nl.id,
            'operation': 'online_redirect',
        })
        tx._apply_updates(parsed_from_form({
            'brq_invoicenumber': reference,
            'brq_amount': str(amount),
            'brq_currency': 'EUR',
            'brq_statuscode': '190',
            'brq_transactions': 'BILLINK_DONE_KEY',
        }))
        self.assertEqual(tx.state, 'done')
        return tx

    def _mock_refund_response(self, key='BL_REFUND_KEY', status_code=190):
        response = make_mock_sdk_response(status_code)
        response.key = key
        return response

    def _patch_refund(self, response):
        PaymentMethod = type(self.env['payment.method'])
        return patch.object(PaymentMethod, '_buckaroo_create_refund', return_value=response)

    def test_refund_creates_linked_child_tx(self):
        tx = self._create_done_tx(reference='BL-REFUND-CREATE')
        with self._patch_refund(self._mock_refund_response()):
            refund_tx = tx._refund()

        self.assertEqual(refund_tx.operation, 'refund')
        self.assertEqual(refund_tx.source_transaction_id, tx)
        self.assertIn('R-', refund_tx.reference)

    def test_refund_status_matrix(self):
        for status_code, expected_state in self.REFUND_STATUS_CASES:
            with self.subTest(status_code=status_code, expected_state=expected_state):
                tx = self._create_done_tx(reference='BL-REFUND-%s' % status_code)
                with self._patch_refund(self._mock_refund_response(
                    key='BL_REFUND_%s' % status_code, status_code=status_code,
                )):
                    refund_tx = tx._refund()
                self.assertEqual(refund_tx.state, expected_state)

    def test_partial_refund_passes_amount_to_sdk(self):
        tx = self._create_done_tx(reference='BL-PARTIAL', amount=100.0)
        with self._patch_refund(self._mock_refund_response()) as mock_refund:
            refund_tx = tx._refund(amount_to_refund=25.0)

        self.assertEqual(abs(refund_tx.amount), 25.0)
        refund_tx_arg = mock_refund.call_args[0][1]
        self.assertEqual(abs(refund_tx_arg.amount), 25.0)

    def test_refund_webhook_transitions_pending_to_done(self):
        for fmt in ('form', 'json'):
            with self.subTest(handler=fmt):
                tx = self._create_done_tx(reference='BL-REFUND-WH-%s' % fmt.upper())
                with self._patch_refund(self._mock_refund_response(status_code=790)):
                    refund_tx = tx._refund()
                self.assertEqual(refund_tx.state, 'pending')

                if fmt == 'form':
                    parsed = parsed_from_form({
                        'brq_invoicenumber': refund_tx.reference,
                        'brq_amount': str(abs(refund_tx.amount)),
                        'brq_currency': 'EUR',
                        'brq_statuscode': '190',
                        'brq_transactions': 'BL_REFUND_KEY',
                    })
                else:
                    parsed = parsed_from_json({
                        'Transaction': {
                            'Invoice': refund_tx.reference,
                            'AmountDebit': abs(refund_tx.amount),
                            'Currency': 'EUR',
                            'Status': {'Code': {'Code': 190, 'Description': 'Success'}},
                            'Key': 'BL_REFUND_KEY',
                        },
                    })
                refund_tx._apply_updates(parsed)
                self.assertEqual(refund_tx.state, 'done')


@tagged('post_install', '-at_install')
class TestBillinkAmountLimitEnforcement(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.billink = cls.env.ref('payment_buckaroo_official.payment_method_billink')
        cls.buckaroo.payment_method_ids = [Command.link(cls.billink.id)]
        cls.billink.buckaroo_official_max_amount = '750.00'
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'NL Partner',
            'country_id': cls.env.ref('base.nl').id,
        })

    def _billink_visible(self, amount):
        methods = self.env['payment.method']._get_compatible_payment_methods(
            self.buckaroo.ids,
            self.partner_nl.id,
            currency_id=self.currency_euro.id,
            amount=amount,
        )
        return self.billink in methods

    def test_below_limit_visible(self):
        self.assertTrue(self._billink_visible(500.0))

    def test_at_limit_visible(self):
        self.assertTrue(self._billink_visible(750.0))

    def test_above_limit_hidden(self):
        self.assertFalse(self._billink_visible(800.0))

    def test_no_limit_always_visible(self):
        self.billink.buckaroo_official_max_amount = ''
        self.assertTrue(self._billink_visible(50000.0))


@tagged('post_install', '-at_install')
class TestBillinkCurrencyRestriction(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.billink = cls.env.ref('payment_buckaroo_official.payment_method_billink')
        cls.buckaroo.payment_method_ids = [Command.link(cls.billink.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'NL', 'country_id': cls.env.ref('base.nl').id,
        })

    def _visible(self, partner, currency=None):
        methods = self.env['payment.method']._get_compatible_payment_methods(
            self.buckaroo.ids,
            partner.id,
            currency_id=(currency or self.currency_euro).id,
            amount=100.0,
        )
        return self.billink in methods

    def test_eur_visible(self):
        self.assertTrue(self._visible(self.partner_nl))

    def test_non_eur_currency_hidden(self):
        self.assertFalse(self._visible(self.partner_nl, currency=self.env.ref('base.USD')))


@tagged('post_install', '-at_install')
class TestBillinkShopPaymentControllerValidations(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.billink = cls.env.ref('payment_buckaroo_official.payment_method_billink')
        cls.buckaroo.payment_method_ids = [Command.link(cls.billink.id)]

    def _invoke(self, **kwargs):
        """Fire the Billink controller handler; it raises before super() is reached."""
        import odoo.http
        from odoo.addons.payment_buckaroo_official.controllers.billink import (
            BillinkPaymentPortal,
        )
        controller = BillinkPaymentPortal()

        mock_request = MagicMock()
        mock_request.env = self.env
        mock_request.session = {}
        with patch.object(odoo.http, 'request', mock_request), patch(
            'odoo.addons.payment_buckaroo_official.controllers.billink.request',
            new=mock_request,
        ):
            return controller.shop_payment_transaction(
                order_id=1,
                access_token='ignored',
                payment_method_id=self.billink.id,
                **kwargs,
            )

    def test_missing_terms_raises(self):
        with self.assertRaises(ValidationError) as ctx:
            self._invoke(billink_birthdate='1990-01-01')
        self.assertIn('Terms and Conditions', str(ctx.exception))

    def test_missing_birthdate_raises(self):
        with self.assertRaises(ValidationError) as ctx:
            self._invoke(billink_tc_accepted=True)
        self.assertIn('date of birth', str(ctx.exception))

    def test_invalid_birthdate_format_raises(self):
        with self.assertRaises(ValidationError) as ctx:
            self._invoke(billink_tc_accepted=True, billink_birthdate='not-a-date')
        self.assertIn('Invalid date of birth', str(ctx.exception))

    def test_under_18_raises(self):
        with self.assertRaises(ValidationError) as ctx:
            self._invoke(billink_tc_accepted=True, billink_birthdate='2020-01-01')
        self.assertIn('18', str(ctx.exception))

    def test_valid_birthdate_persists_on_logged_in_user_partner(self):
        """Logged-in user → write to partner so next checkout can prefill."""
        from datetime import date
        partner = self.env.user.partner_id
        partner.buckaroo_billink_birthdate = False
        with patch(
            'odoo.addons.website_sale.controllers.payment.PaymentPortal.shop_payment_transaction',
            return_value='SUPER_OK',
        ):
            self._invoke(billink_tc_accepted=True, billink_birthdate='1990-05-15')
        self.assertEqual(partner.buckaroo_billink_birthdate, date(1990, 5, 15))


@tagged('post_install', '-at_install')
class TestBillinkCreatePaymentDispatch(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.billink = cls.env.ref('payment_buckaroo_official.payment_method_billink')
        cls.buckaroo.payment_method_ids = [Command.link(cls.billink.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan',
            'street': 'Keizersgracht 424',
            'zip': '1016 GC',
            'city': 'Amsterdam',
            'country_id': cls.env.ref('base.nl').id,
            'email': 'jan@example.nl',
            'phone': '+31612345678',
        })

    def test_non_billink_skips_add_parameter(self):
        tx = self._create_buckaroo_tx(reference='TX-NB-001', payment_method=self.ideal)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch('odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService') as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        mock_builder.add_parameter.assert_not_called()
        self.assertEqual(result, mock_response)

    def test_billink_adds_article_and_customer_parameters(self):
        tx = self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.billink.id,
            'reference': 'TX-BL-001',
            'amount': 50.0,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner_nl.id,
            'operation': 'online_redirect',
        })
        product = self.env['product.product'].create({
            'name': 'Widget',
            'default_code': 'WIDGET-01',
            'list_price': 50.0,
        })
        order = self.env['sale.order'].create({'partner_id': self.partner_nl.id})
        self.env['sale.order.line'].create({
            'order_id': order.id,
            'product_id': product.id,
            'product_uom_qty': 1,
            'price_unit': 50.0,
        })
        tx.sale_order_ids = [Command.link(order.id)]

        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_billink.PaymentService'
        ) as MockPS, patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_billink.'
            'PaymentMethodBillink._get_birthdate_from_session',
            return_value='',
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.billink._buckaroo_create_payment(tx, client)

        param_names = [call[0][0] for call in mock_builder.add_parameter.call_args_list]
        self.assertIn('article', param_names)
        self.assertIn('billingCustomer', param_names)
        self.assertIn('shippingCustomer', param_names)
        mock_builder.pay.assert_called_once()
        self.assertEqual(result, mock_response)


@tagged('post_install', '-at_install')
class TestBillinkBirthdateSession(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.billink = cls.env.ref('payment_buckaroo_official.payment_method_billink')
        cls.buckaroo.payment_method_ids = [Command.link(cls.billink.id)]

    def test_valid_date_converted_and_popped(self):
        mock_session = {'buckaroo_billink_birthdate': '1990-05-15'}
        mock_request = MagicMock()
        mock_request.session = mock_session

        with patch.dict('sys.modules', {'odoo.http': MagicMock(request=mock_request)}):
            result = self.billink._get_birthdate_from_session()

        self.assertEqual(result, '15-05-1990')
        self.assertNotIn('buckaroo_billink_birthdate', mock_session)

    def test_invalid_date_returns_empty(self):
        mock_session = {'buckaroo_billink_birthdate': 'not-a-date'}
        mock_request = MagicMock()
        mock_request.session = mock_session

        with patch.dict('sys.modules', {'odoo.http': MagicMock(request=mock_request)}):
            self.assertEqual(self.billink._get_birthdate_from_session(), '')

    def test_no_request_returns_empty(self):
        with patch.dict('sys.modules', {'odoo.http': MagicMock(request=None)}):
            self.assertEqual(self.billink._get_birthdate_from_session(), '')

    def test_proxy_runtime_error_returns_empty(self):
        # Odoo's request is a LocalProxy; accessing it outside HTTP context
        # raises RuntimeError rather than returning None.
        proxy_mock = MagicMock()
        proxy_mock.session.pop.side_effect = RuntimeError("no request context")

        with patch.dict('sys.modules', {'odoo.http': MagicMock(request=proxy_mock)}):
            self.assertEqual(self.billink._get_birthdate_from_session(), '')


class TestBillinkOrderLineFormatting(BaseCase):

    def _make_tx(self, lines, amount=None):
        total = amount if amount is not None else sum(
            l.price_total for l in lines if not l.display_type
        )
        tx = MagicMock()
        tx.amount = total
        order = MagicMock()
        order.order_line = lines
        tx.sale_order_ids = [order]
        tx.partner_id = make_partner()
        return tx

    def test_identifier_uses_default_code(self):
        line = _make_mock_order_line(product_code='MY-SKU', product_id_val=99)
        articles = get_order_articles(self._make_tx([line]))
        self.assertEqual(articles[0]['identifier'], 'MY-SKU')

    def test_identifier_falls_back_to_product_id(self):
        line = _make_mock_order_line(product_code=None, product_id_val=42)
        articles = get_order_articles(self._make_tx([line]))
        self.assertEqual(articles[0]['identifier'], '42')

    def test_description_uses_line_name(self):
        line = _make_mock_order_line(name='Special Widget')
        articles = get_order_articles(self._make_tx([line]))
        self.assertEqual(articles[0]['description'], 'Special Widget')

    def test_unit_prices_are_per_unit(self):
        line = _make_mock_order_line(qty=2.0, price_total=24.20, price_subtotal=20.0)
        articles = get_order_articles(self._make_tx([line]))
        self.assertAlmostEqual(articles[0]['unit_price_incl'], 12.10, places=2)
        self.assertAlmostEqual(articles[0]['unit_price_excl'], 10.0, places=2)

    def test_vat_percentage_from_price_tax(self):
        line = _make_mock_order_line(
            qty=1.0, price_total=12.10, price_subtotal=10.0, tax_amount=21.0,
        )
        articles = get_order_articles(self._make_tx([line]))
        self.assertAlmostEqual(articles[0]['vat_percentage'], 21.0, places=2)

    def test_section_lines_excluded(self):
        normal = _make_mock_order_line(
            product_code='ITEM', qty=1.0, price_total=10.0, price_subtotal=10.0, tax_amount=0.0,
        )
        section = _make_mock_order_line(display_type='line_section')
        articles = get_order_articles(self._make_tx([normal, section], amount=10.0))
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]['identifier'], 'ITEM')

    def test_rounding_correction_appended_on_mismatch(self):
        line1 = _make_mock_order_line(
            product_code='A', qty=1.0, price_total=4.99, price_subtotal=4.99, tax_amount=0.0,
        )
        line2 = _make_mock_order_line(
            product_code='B', qty=1.0, price_total=5.00, price_subtotal=5.00, tax_amount=0.0,
        )
        articles = get_order_articles(self._make_tx([line1, line2], amount=10.00))
        self.assertEqual(len(articles), 3)
        self.assertEqual(articles[-1]['identifier'], 'rounding')
        self.assertEqual(articles[-1]['type'], 'rounding')
        self.assertAlmostEqual(articles[-1]['unit_price_incl'], 0.01, places=2)

    def test_no_rounding_when_total_matches(self):
        line = _make_mock_order_line(
            product_code='EXACT', qty=2.0, price_total=20.0, price_subtotal=20.0, tax_amount=0.0,
        )
        articles = get_order_articles(self._make_tx([line], amount=20.0))
        self.assertEqual(len(articles), 1)
        self.assertNotEqual(articles[0]['identifier'], 'rounding')

    def test_no_sale_orders_returns_empty(self):
        tx = MagicMock()
        tx.amount = 0.0
        tx.sale_order_ids = []
        self.assertEqual(get_order_articles(tx), [])


class TestFormatBillinkArticles(BaseCase):

    def test_maps_generic_dict_to_billink_pascalcase(self):
        generic = [{
            'identifier': 'SKU-1', 'description': 'Widget', 'quantity': 2.0,
            'unit_price_incl': 12.10, 'unit_price_excl': 10.0,
            'vat_percentage': 21.0, 'vat_amount': 4.20, 'type': 'product',
        }]
        result = BillinkPaymentMethod._format_billink_articles(generic)
        self.assertEqual(len(result), 1)
        a = result[0]
        self.assertEqual(a['Identifier'], 'SKU-1')
        self.assertEqual(a['Description'], 'Widget')
        self.assertEqual(a['Quantity'], '2')
        self.assertAlmostEqual(a['GrossUnitPriceIncl'], 12.10, places=2)
        self.assertAlmostEqual(a['GrossUnitPriceExcl'], 10.0, places=2)
        self.assertAlmostEqual(a['VatPercentage'], 21.0, places=2)


class TestFormatBillinkCustomer(BaseCase):

    def test_b2c_customer(self):
        data = get_customer_data(make_partner(
            name='Jan de Vries', street='Keizersgracht 424',
            zip_code='1016 GC', city='Amsterdam', country_code='NL',
            email='jan@example.com', phone='+31612345678',
        ))
        result = BillinkPaymentMethod._format_billink_customer(data)
        self.assertEqual(result['Category'], 'B2C')
        self.assertEqual(result['FirstName'], 'Jan')
        self.assertEqual(result['LastName'], 'de Vries')
        self.assertEqual(result['Initials'], 'J.D.V.')
        self.assertEqual(result['Street'], 'Keizersgracht')
        self.assertEqual(result['StreetNumber'], '424')
        self.assertEqual(result['PostalCode'], '1016 GC')
        self.assertEqual(result['City'], 'Amsterdam')
        self.assertEqual(result['Country'], 'NL')
        self.assertEqual(result['Email'], 'jan@example.com')
        self.assertEqual(result['MobilePhone'], '+31612345678')
        self.assertEqual(result['CareOf'], '')
        self.assertNotIn('ChamberOfCommerce', result)

    def test_b2b_customer(self):
        data = get_customer_data(make_partner(
            is_company=True, commercial_company_name='Acme BV',
            company_registry='12345678',
        ))
        result = BillinkPaymentMethod._format_billink_customer(data)
        self.assertEqual(result['Category'], 'B2B')
        self.assertEqual(result['CareOf'], 'Acme BV')
        self.assertEqual(result['ChamberOfCommerce'], '12345678')
