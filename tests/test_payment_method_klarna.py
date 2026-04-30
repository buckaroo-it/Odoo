# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""End-to-end + unit tests for the Klarna payment method.

Covers payment creation (asserts SDK ``.reserve()`` is called, NOT ``.pay()``),
webhook state transitions (form + JSON), refund lifecycle, amount/country
gating, klarna-only dispatch on payment.method, the dict→Klarna API
formatters, and the gender-from-session flow. SDK transport is mocked
throughout.
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
from ..helpers.customer import get_customer_data
from ..models.payment_method_klarna import PaymentMethodKlarna as KlarnaPaymentMethod
from .test_helpers import make_partner


def _set_session_gender(value='1'):
    """Helper to stub ``odoo.http.request`` with a session carrying gender."""
    session = {'buckaroo_klarna_gender': value} if value is not None else {}
    fake_request = MagicMock()
    fake_request.session = session
    return patch(
        'odoo.addons.payment_buckaroo_official.models.payment_method_klarna.'
        'http_request' if False else 'odoo.http.request',
        fake_request,
    )


@tagged('post_install', '-at_install')
class TestKlarnaPaymentCreation(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.klarna = cls.env.ref('payment_buckaroo_official.payment_method_klarna')
        cls.buckaroo.payment_method_ids = [Command.link(cls.klarna.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan de Vries',
            'country_id': cls.env.ref('base.nl').id,
            'email': 'jan@example.nl',
        })

    def _sdk_response(self, redirect_url='https://testcheckout.buckaroo.nl/pay/KL',
                      key='KL_KEY'):
        response = MagicMock()
        response.get_redirect_url.return_value = redirect_url
        response.key = key
        response.status_code = 200
        response.redirect_url = redirect_url
        response.required_action = None
        response.buckaroo_status_message = 'Success'
        response._raw_data = {}
        return response

    def _create_tx(self, reference='KLARNA-TX-001', amount=100.0):
        return self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.klarna.id,
            'reference': reference,
            'amount': amount,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner_nl.id,
            'operation': 'online_redirect',
        })

    def test_payment_creation_returns_redirect_and_stores_reference(self):
        response = self._sdk_response(redirect_url='https://kl/pay/X', key='STORED_KEY')
        tx = self._create_tx()
        PaymentMethod = type(tx.payment_method_id)

        with patch.object(PaymentMethod, '_buckaroo_create_payment', return_value=response), \
             patch.object(PaymentMethod, '_buckaroo_extract_redirect_url',
                          return_value='https://kl/pay/X'):
            result = tx._get_specific_processing_values({})

        self.assertEqual(result['api_url'], 'https://kl/pay/X')
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
class TestKlarnaWebhookCallbacks(BuckarooOfficialCommon):

    CALLBACK_DONE_CODES = [190]
    CALLBACK_PENDING_CODES = [790, 791, 792, 793]
    CALLBACK_CANCEL_CODES = [890, 891]
    CALLBACK_ERROR_CODES = [490, 690]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.klarna = cls.env.ref('payment_buckaroo_official.payment_method_klarna')
        cls.buckaroo.payment_method_ids = [Command.link(cls.klarna.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan de Vries',
            'country_id': cls.env.ref('base.nl').id,
        })

    def _create_tx(self, reference, amount=100.0):
        return self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.klarna.id,
            'reference': reference,
            'amount': amount,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner_nl.id,
            'operation': 'online_redirect',
        })

    def _form_callback(self, reference, status_code, txn_key='KLARNA_TXN_001'):
        return parsed_from_form({
            'brq_invoicenumber': reference,
            'brq_amount': '100.00',
            'brq_currency': 'EUR',
            'brq_statuscode': str(status_code),
            'brq_transactions': txn_key,
        })

    def _json_callback(self, reference, status_code, txn_key='KLARNA_TXN_001'):
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
                    ref = 'KLARNA-WH-%s-%s' % (fmt.upper(), code)
                    tx = self._create_tx(ref)
                    builder = self._form_callback if fmt == 'form' else self._json_callback
                    parsed = builder(ref, code)
                    self.env['payment.transaction'].sudo()._process('buckaroo_official', parsed)
                    self.assertEqual(tx.state, expected_state)

    def test_done_status_maps_to_done(self):
        self._assert_status_maps_to_state(self.CALLBACK_DONE_CODES, 'done')
        tx = self._create_tx('KLARNA-WH-REF-190')
        parsed = self._form_callback('KLARNA-WH-REF-190', 190)
        self.env['payment.transaction'].sudo()._process('buckaroo_official', parsed)
        self.assertEqual(tx.provider_reference, 'KLARNA_TXN_001')

    def test_pending_status_maps_to_pending(self):
        self._assert_status_maps_to_state(self.CALLBACK_PENDING_CODES, 'pending')

    def test_cancel_status_maps_to_cancel(self):
        self._assert_status_maps_to_state(self.CALLBACK_CANCEL_CODES, 'cancel')

    def test_error_status_maps_to_error(self):
        self._assert_status_maps_to_state(self.CALLBACK_ERROR_CODES, 'error')

    def test_e2e_creation_to_success_callback_sets_authorized(self):
        """Klarna MOR is Reserve→Pay; a 190 callback after Reserve must
        transition to 'authorized', not 'done'. Capture (Pay) happens later."""
        redirect_url = 'https://testcheckout.buckaroo.nl/pay/E2E'
        response = MagicMock()
        response.get_redirect_url.return_value = redirect_url
        response.key = 'E2E_KEY'
        response.status_code = 200
        response.redirect_url = redirect_url
        response.required_action = None
        response.buckaroo_status_message = 'Success'
        response._raw_data = {}

        tx = self._create_tx('KLARNA-E2E-001')
        PaymentMethod = type(tx.payment_method_id)
        with patch.object(PaymentMethod, '_buckaroo_create_payment', return_value=response), \
             patch.object(PaymentMethod, '_buckaroo_extract_redirect_url', return_value=redirect_url):
            result = tx._get_specific_processing_values({})

        self.assertEqual(result['api_url'], redirect_url)
        self.assertEqual(tx.provider_reference, 'E2E_KEY')
        self.assertEqual(tx.buckaroo_official_payment_action, 'authorize')

        callback = self._form_callback('KLARNA-E2E-001', 190, txn_key='E2E_KEY')
        self.env['payment.transaction'].sudo()._process('buckaroo_official', callback)
        self.assertEqual(tx.state, 'authorized')


@tagged('post_install', '-at_install')
class TestKlarnaRefundFlow(BuckarooOfficialCommon):

    REFUND_STATUS_CASES = [
        (190, 'done'),
        (790, 'pending'),
        (890, 'cancel'),
        (490, 'error'),
    ]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.klarna = cls.env.ref('payment_buckaroo_official.payment_method_klarna')
        cls.buckaroo.payment_method_ids = [Command.link(cls.klarna.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan de Vries',
            'country_id': cls.env.ref('base.nl').id,
        })

    def _create_done_tx(self, reference='KL-DONE', amount=100.0):
        tx = self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.klarna.id,
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
            'brq_transactions': 'KLARNA_DONE_KEY',
        }))
        self.assertEqual(tx.state, 'done')
        return tx

    def _mock_refund_response(self, key='KL_REFUND_KEY', status_code=190):
        response = make_mock_sdk_response(status_code)
        response.key = key
        return response

    def _patch_refund(self, response):
        PaymentMethod = type(self.env['payment.method'])
        return patch.object(PaymentMethod, '_buckaroo_create_refund', return_value=response)

    def test_refund_creates_linked_child_tx(self):
        tx = self._create_done_tx(reference='KL-REFUND-CREATE')
        with self._patch_refund(self._mock_refund_response()):
            refund_tx = tx._refund()

        self.assertEqual(refund_tx.operation, 'refund')
        self.assertEqual(refund_tx.source_transaction_id, tx)
        self.assertIn('R-', refund_tx.reference)

    def test_refund_status_matrix(self):
        for status_code, expected_state in self.REFUND_STATUS_CASES:
            with self.subTest(status_code=status_code, expected_state=expected_state):
                tx = self._create_done_tx(reference='KL-REFUND-%s' % status_code)
                with self._patch_refund(self._mock_refund_response(
                    key='KL_REFUND_%s' % status_code, status_code=status_code,
                )):
                    refund_tx = tx._refund()
                self.assertEqual(refund_tx.state, expected_state)

    def test_partial_refund_passes_amount_to_sdk(self):
        tx = self._create_done_tx(reference='KL-PARTIAL', amount=100.0)
        with self._patch_refund(self._mock_refund_response()) as mock_refund:
            refund_tx = tx._refund(amount_to_refund=25.0)

        self.assertEqual(abs(refund_tx.amount), 25.0)
        refund_tx_arg = mock_refund.call_args[0][1]
        self.assertEqual(abs(refund_tx_arg.amount), 25.0)

    def test_refund_webhook_transitions_pending_to_done(self):
        for fmt in ('form', 'json'):
            with self.subTest(handler=fmt):
                tx = self._create_done_tx(reference='KL-REFUND-WH-%s' % fmt.upper())
                with self._patch_refund(self._mock_refund_response(status_code=790)):
                    refund_tx = tx._refund()
                self.assertEqual(refund_tx.state, 'pending')

                if fmt == 'form':
                    parsed = parsed_from_form({
                        'brq_invoicenumber': refund_tx.reference,
                        'brq_amount': str(abs(refund_tx.amount)),
                        'brq_currency': 'EUR',
                        'brq_statuscode': '190',
                        'brq_transactions': 'KL_REFUND_KEY',
                    })
                else:
                    parsed = parsed_from_json({
                        'Transaction': {
                            'Invoice': refund_tx.reference,
                            'AmountDebit': abs(refund_tx.amount),
                            'Currency': 'EUR',
                            'Status': {'Code': {'Code': 190, 'Description': 'Success'}},
                            'Key': 'KL_REFUND_KEY',
                        },
                    })
                refund_tx._apply_updates(parsed)
                self.assertEqual(refund_tx.state, 'done')


@tagged('post_install', '-at_install')
class TestKlarnaAmountLimitEnforcement(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.klarna = cls.env.ref('payment_buckaroo_official.payment_method_klarna')
        cls.buckaroo.payment_method_ids = [Command.link(cls.klarna.id)]
        cls.klarna.buckaroo_official_max_amount = '750.00'
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'NL Partner',
            'country_id': cls.env.ref('base.nl').id,
        })

    def _klarna_visible(self, amount):
        methods = self.env['payment.method']._get_compatible_payment_methods(
            self.buckaroo.ids,
            self.partner_nl.id,
            currency_id=self.currency_euro.id,
            amount=amount,
        )
        return self.klarna in methods

    def test_below_limit_visible(self):
        self.assertTrue(self._klarna_visible(500.0))

    def test_at_limit_visible(self):
        self.assertTrue(self._klarna_visible(750.0))

    def test_above_limit_hidden(self):
        self.assertFalse(self._klarna_visible(800.0))

    def test_no_limit_always_visible(self):
        self.klarna.buckaroo_official_max_amount = ''
        self.assertTrue(self._klarna_visible(50000.0))


@tagged('post_install', '-at_install')
class TestKlarnaCurrencyRestriction(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.klarna = cls.env.ref('payment_buckaroo_official.payment_method_klarna')
        cls.buckaroo.payment_method_ids = [Command.link(cls.klarna.id)]
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
        return self.klarna in methods

    def test_eur_visible(self):
        self.assertTrue(self._visible(self.partner_nl))

    def test_usd_currency_hidden(self):
        self.assertFalse(self._visible(self.partner_nl, currency=self.env.ref('base.USD')))


@tagged('post_install', '-at_install')
class TestKlarnaCreatePaymentDispatch(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.klarna = cls.env.ref('payment_buckaroo_official.payment_method_klarna')
        cls.buckaroo.payment_method_ids = [Command.link(cls.klarna.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan',
            'street': 'Keizersgracht 424',
            'zip': '1016 GC',
            'city': 'Amsterdam',
            'country_id': cls.env.ref('base.nl').id,
            'email': 'jan@example.nl',
            'phone': '+31612345678',
        })

    def test_non_klarna_skips_add_parameter_and_calls_pay(self):
        tx = self._create_buckaroo_tx(reference='TX-NB-001', payment_method=self.ideal)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch('odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService') as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        mock_builder.reserve.assert_not_called()
        mock_builder.add_parameter.assert_not_called()
        self.assertEqual(result, mock_response)

    def test_klarna_calls_reserve_with_article_and_customer_parameters(self):
        tx = self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.klarna.id,
            'reference': 'TX-KL-001',
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

        fake_request = MagicMock()
        fake_request.session = {'buckaroo_klarna_gender': '1'}
        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_klarna.PaymentService'
        ) as MockPS, patch(
            'odoo.http.request', fake_request,
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.klarna._buckaroo_create_payment(tx, client)

        params_by_name = {
            call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list
        }
        self.assertIn('article', params_by_name)
        self.assertEqual(params_by_name['gender'], 1)
        self.assertEqual(params_by_name['operatingCountry'], 'NL')
        self.assertEqual(params_by_name['shippingSameAsBilling'], 'true')
        # Flat Billing*/Shipping* params, not grouped customer objects.
        self.assertEqual(params_by_name['BillingStreet'], 'Keizersgracht')
        self.assertEqual(params_by_name['BillingHouseNumber'], '424')
        self.assertEqual(params_by_name['BillingPostalCode'], '1016 GC')
        self.assertEqual(params_by_name['BillingCellPhoneNumber'], '31612345678')
        self.assertEqual(params_by_name['ShippingStreet'], 'Keizersgracht')

        # article payload uses Klarna's articleNumber/articleTitle keys.
        article = params_by_name['article'][0]
        self.assertEqual(article['articleTitle'], '[WIDGET-01] Widget')
        self.assertEqual(article['articleQuantity'], '1')

        mock_builder.reserve.assert_called_once()
        mock_builder.pay.assert_not_called()
        self.assertEqual(result, mock_response)


@tagged('post_install', '-at_install')
class TestKlarnaPaymentAction(BuckarooOfficialCommon):
    """_buckaroo_get_payment_action override for klarna."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.klarna = cls.env.ref('payment_buckaroo_official.payment_method_klarna')
        cls.buckaroo.payment_method_ids = [Command.link(cls.klarna.id)]

    def test_klarna_returns_authorize(self):
        """Klarna MOR has no immediate-capture path; always 'authorize'."""
        self.assertEqual(self.klarna._buckaroo_get_payment_action(), 'authorize')

    def test_non_klarna_delegates_to_base(self):
        """Other methods must not be affected by the klarna override."""
        self.assertIsNone(self.ideal._buckaroo_get_payment_action())


@tagged('post_install', '-at_install')
class TestKlarnaAuthorizeWebhookSetsAuthorizedState(BuckarooOfficialCommon):
    """When the tx has buckaroo_official_payment_action='authorize', a 190
    callback must transition to 'authorized' (not 'done')."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.klarna = cls.env.ref('payment_buckaroo_official.payment_method_klarna')
        cls.buckaroo.payment_method_ids = [Command.link(cls.klarna.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan',
            'country_id': cls.env.ref('base.nl').id,
        })

    def test_e2e_creation_to_success_callback_sets_authorized(self):
        redirect_url = 'https://testcheckout.buckaroo.nl/pay/RESERVE'
        response = MagicMock()
        response.get_redirect_url.return_value = redirect_url
        response.key = 'RES_KEY_E2E'
        response.status_code = 200
        response.redirect_url = redirect_url
        response.required_action = None
        response.buckaroo_status_message = 'Success'
        response._raw_data = {}

        tx = self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.klarna.id,
            'reference': 'KLARNA-AUTHORIZED-E2E',
            'amount': 100.0,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner_nl.id,
            'operation': 'online_redirect',
        })

        PaymentMethod = type(tx.payment_method_id)
        with patch.object(PaymentMethod, '_buckaroo_create_payment', return_value=response), \
             patch.object(PaymentMethod, '_buckaroo_extract_redirect_url', return_value=redirect_url):
            tx._get_specific_processing_values({})

        self.assertEqual(tx.buckaroo_official_payment_action, 'authorize')
        self.assertEqual(tx.provider_reference, 'RES_KEY_E2E')

        callback = parsed_from_form({
            'brq_invoicenumber': 'KLARNA-AUTHORIZED-E2E',
            'brq_amount': '100.00',
            'brq_currency': 'EUR',
            'brq_statuscode': '190',
            'brq_transactions': 'RES_KEY_E2E',
        })
        self.env['payment.transaction'].sudo()._process('buckaroo_official', callback)
        self.assertEqual(tx.state, 'authorized')


@tagged('post_install', '-at_install')
class TestKlarnaCaptureAndVoid(BuckarooOfficialCommon):
    """_buckaroo_create_capture/void should use SDK pay/cancelReservation
    with the right keys for klarna."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.klarna = cls.env.ref('payment_buckaroo_official.payment_method_klarna')
        cls.buckaroo.payment_method_ids = [Command.link(cls.klarna.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan',
            'country_id': cls.env.ref('base.nl').id,
        })

    def _create_authorized_tx(self, reference='KL-AUTH', provider_reference='RES_KEY_1'):
        tx = self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.klarna.id,
            'reference': reference,
            'amount': 100.0,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner_nl.id,
            'operation': 'online_redirect',
        })
        tx.provider_reference = provider_reference
        tx.buckaroo_official_payment_action = 'authorize'
        return tx

    def test_capture_calls_sdk_pay_with_data_request_key(self):
        """Klarna pay-as-capture passes the prior Reserve key as
        DataRequestKey, not OriginalTransactionKey."""
        tx = self._create_authorized_tx(provider_reference='RES_KEY_CAP')
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_klarna.PaymentService'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.klarna._buckaroo_create_capture(tx, client)

        mock_builder.pay.assert_called_once_with()
        mock_builder.cancelReservation.assert_not_called()
        # Capture only attaches dataRequestKey; cart/customer params are
        # carried over server-side from the Reserve.
        mock_builder.add_parameter.assert_called_once_with('dataRequestKey', 'RES_KEY_CAP')
        self.assertEqual(result, mock_response)

    def test_void_calls_sdk_cancel_reservation_with_original_transaction_key(self):
        tx = self._create_authorized_tx(provider_reference='RES_KEY_VOID')
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_klarna.PaymentService'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.klarna._buckaroo_create_void(tx, client)

        mock_builder.cancelReservation.assert_called_once_with(
            original_transaction_key='RES_KEY_VOID',
        )
        mock_builder.cancelAuthorize.assert_not_called()
        mock_builder.add_parameter.assert_not_called()
        self.assertEqual(result, mock_response)

    def test_non_klarna_capture_delegates_to_super(self):
        """iDEAL (non-klarna) capture should still use SDK .capture()."""
        tx = self._create_buckaroo_tx(reference='TX-IDEAL-CAP')
        tx.provider_reference = 'IDEAL_AUTH_KEY'
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.ideal._buckaroo_create_capture(tx, client)

        mock_builder.capture.assert_called_once()
        mock_builder.pay.assert_not_called()

    def test_non_klarna_void_delegates_to_super(self):
        """iDEAL (non-klarna) void should still use SDK .cancelAuthorize()."""
        tx = self._create_buckaroo_tx(reference='TX-IDEAL-VOID')
        tx.provider_reference = 'IDEAL_AUTH_KEY'
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.ideal._buckaroo_create_void(tx, client)

        mock_builder.cancelAuthorize.assert_called_once()
        mock_builder.cancelReservation.assert_not_called()


@tagged('post_install', '-at_install')
class TestKlarnaSendCaptureVoidRequest(BuckarooOfficialCommon):
    """Full transaction-level _send_capture_request / _send_void_request flows."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.klarna = cls.env.ref('payment_buckaroo_official.payment_method_klarna')
        cls.buckaroo.payment_method_ids = [Command.link(cls.klarna.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan',
            'country_id': cls.env.ref('base.nl').id,
        })

    def _create_authorized_tx(self, reference='KL-SEND', provider_reference='KL_AUTH_KEY'):
        tx = self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.klarna.id,
            'reference': reference,
            'amount': 100.0,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner_nl.id,
            'operation': 'online_redirect',
        })
        tx.provider_reference = provider_reference
        tx.buckaroo_official_payment_action = 'authorize'
        return tx

    def test_send_capture_request_invokes_klarna_capture(self):
        tx = self._create_authorized_tx(reference='KL-SEND-CAP')
        PaymentMethod = type(tx.payment_method_id)
        mock_response = make_mock_sdk_response(190)

        with patch.object(
            PaymentMethod, '_buckaroo_create_capture', return_value=mock_response,
        ) as mock_capture, \
             patch.object(
                 type(tx.provider_id),
                 '_buckaroo_official_get_client',
                 return_value=MagicMock(),
             ):
            tx._send_capture_request()

        mock_capture.assert_called_once()

    def test_send_void_request_invokes_klarna_void(self):
        tx = self._create_authorized_tx(reference='KL-SEND-VOID')
        PaymentMethod = type(tx.payment_method_id)
        mock_response = make_mock_sdk_response(190)

        with patch.object(
            PaymentMethod, '_buckaroo_create_void', return_value=mock_response,
        ) as mock_void, \
             patch.object(
                 type(tx.provider_id),
                 '_buckaroo_official_get_client',
                 return_value=MagicMock(),
             ):
            tx._send_void_request()

        mock_void.assert_called_once()

    def test_capture_via_full_dispatch_calls_sdk_pay_with_data_request_key(self):
        """Full pipeline: provider_reference → _send_capture_request →
        SDK builder.pay(data_request_key=...). Asserts the mocked SDK
        receives the prior Reserve key as DataRequestKey."""
        tx = self._create_authorized_tx(reference='KL-FULL-CAP', provider_reference='RES_FULL_CAP')
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_klarna.PaymentService'
        ) as MockPS, patch.object(
            type(tx.provider_id),
            '_buckaroo_official_get_client',
            return_value=MagicMock(),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            tx._send_capture_request()

        mock_builder.add_parameter.assert_called_once_with('dataRequestKey', 'RES_FULL_CAP')
        mock_builder.pay.assert_called_once_with()

    def test_void_via_full_dispatch_calls_sdk_cancel_reservation(self):
        """Full pipeline: provider_reference → _send_void_request →
        SDK builder.cancelReservation(original_transaction_key=...)."""
        tx = self._create_authorized_tx(reference='KL-FULL-VOID', provider_reference='RES_FULL_VOID')
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_klarna.PaymentService'
        ) as MockPS, patch.object(
            type(tx.provider_id),
            '_buckaroo_official_get_client',
            return_value=MagicMock(),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            tx._send_void_request()

        mock_builder.cancelReservation.assert_called_once_with(
            original_transaction_key='RES_FULL_VOID',
        )


@tagged('post_install', '-at_install')
class TestKlarnaGenderController(BuckarooOfficialCommon):
    """Controller-level capture of klarna_gender into the HTTP session."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.klarna = cls.env.ref('payment_buckaroo_official.payment_method_klarna')
        cls.buckaroo.payment_method_ids = [Command.link(cls.klarna.id)]

    def _call_controller(self, **kwargs):
        import odoo.http
        from ..controllers.klarna import KlarnaPaymentPortal
        portal = KlarnaPaymentPortal()
        fake_req = MagicMock()
        fake_req.env = self.env
        fake_req.session = {}
        with patch(
            'odoo.addons.website_sale.controllers.payment.PaymentPortal.shop_payment_transaction',
            return_value='SUPER_OK',
        ) as super_stub, patch.object(
            odoo.http, 'request', fake_req,
        ), patch(
            'odoo.addons.payment_buckaroo_official.controllers.klarna.request',
            new=fake_req,
        ):
            result = portal.shop_payment_transaction(
                order_id=1, access_token='tok', **kwargs,
            )
            return result, super_stub, fake_req

    def test_missing_gender_raises(self):
        with self.assertRaises(ValidationError):
            self._call_controller(payment_method_id=self.klarna.id)

    def test_invalid_gender_raises(self):
        with self.assertRaises(ValidationError):
            self._call_controller(
                payment_method_id=self.klarna.id, klarna_gender='9',
            )

    def test_non_int_gender_raises(self):
        with self.assertRaises(ValidationError):
            self._call_controller(
                payment_method_id=self.klarna.id, klarna_gender='not-a-number',
            )

    def test_valid_gender_stored_and_popped_before_super(self):
        result, super_stub, fake_req = self._call_controller(
            payment_method_id=self.klarna.id, klarna_gender='1',
        )
        self.assertEqual(result, 'SUPER_OK')
        self.assertEqual(fake_req.session.get('buckaroo_klarna_gender'), '1')
        # klarna_gender must not leak through to super.
        _, kwargs_to_super = super_stub.call_args
        self.assertNotIn('klarna_gender', kwargs_to_super)

    def test_non_klarna_method_passes_through(self):
        result, super_stub, fake_req = self._call_controller(
            payment_method_id=self.ideal.id,
        )
        self.assertEqual(result, 'SUPER_OK')
        self.assertNotIn('buckaroo_klarna_gender', fake_req.session)

    def test_valid_gender_persists_on_logged_in_user_partner(self):
        """Logged-in user → write to partner so next checkout can prefill."""
        partner = self.env.user.partner_id
        partner.buckaroo_klarna_gender = False
        self._call_controller(
            payment_method_id=self.klarna.id, klarna_gender='2',
        )
        self.assertEqual(partner.buckaroo_klarna_gender, '2')


class TestGetGenderFromSession(BaseCase):

    def test_returns_int_when_session_set(self):
        fake_request = MagicMock()
        fake_request.session = {'buckaroo_klarna_gender': '1'}
        with patch('odoo.http.request', fake_request):
            self.assertEqual(KlarnaPaymentMethod._get_gender_from_session(), 1)

    def test_pops_value_from_session(self):
        fake_request = MagicMock()
        fake_request.session = {'buckaroo_klarna_gender': '2'}
        with patch('odoo.http.request', fake_request):
            KlarnaPaymentMethod._get_gender_from_session()
        self.assertNotIn('buckaroo_klarna_gender', fake_request.session)

    def test_raises_when_missing(self):
        fake_request = MagicMock()
        fake_request.session = {}
        with patch('odoo.http.request', fake_request):
            with self.assertRaises(ValidationError):
                KlarnaPaymentMethod._get_gender_from_session()

    def test_raises_when_no_request(self):
        with patch('odoo.http.request', None):
            with self.assertRaises(ValidationError):
                KlarnaPaymentMethod._get_gender_from_session()


class TestFormatKlarnaArticles(BaseCase):

    def test_maps_generic_dict_to_klarna_keys(self):
        generic = [{
            'identifier': 'SKU-1', 'description': 'Widget', 'quantity': 2.0,
            'unit_price_incl': 12.10, 'unit_price_excl': 10.0,
            'vat_percentage': 21.0, 'vat_amount': 4.20, 'type': 'product',
        }]
        result = KlarnaPaymentMethod._format_klarna_articles(generic)
        self.assertEqual(len(result), 1)
        a = result[0]
        # Klarna SDK expects: articleNumber/articleTitle/articleQuantity/articlePrice/articleVat
        self.assertEqual(a['articleNumber'], 'SKU-1')
        self.assertEqual(a['articleTitle'], 'Widget')
        self.assertEqual(a['articleQuantity'], '2')
        # Klarna takes the gross unit price (incl VAT)
        self.assertEqual(a['articlePrice'], 12.10)
        self.assertEqual(a['articleVat'], 21.0)

    def test_maps_multiple_articles(self):
        generic = [
            {'identifier': 'A', 'description': 'A', 'quantity': 1.0,
             'unit_price_incl': 10.0, 'unit_price_excl': 10.0,
             'vat_percentage': 0.0, 'vat_amount': 0.0, 'type': 'product'},
            {'identifier': 'B', 'description': 'B', 'quantity': 3.0,
             'unit_price_incl': 5.0, 'unit_price_excl': 5.0,
             'vat_percentage': 0.0, 'vat_amount': 0.0, 'type': 'product'},
        ]
        result = KlarnaPaymentMethod._format_klarna_articles(generic)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]['articleTitle'], 'B')
        self.assertEqual(result[1]['articleQuantity'], '3')

    def test_zero_price_articles_are_filtered(self):
        """Klarna rejects zero-price line items; the formatter drops them."""
        generic = [
            {'identifier': 'PAID', 'description': 'Paid', 'quantity': 1.0,
             'unit_price_incl': 10.0, 'unit_price_excl': 10.0,
             'vat_percentage': 0.0, 'vat_amount': 0.0, 'type': 'product'},
            {'identifier': 'FREE', 'description': 'Free', 'quantity': 1.0,
             'unit_price_incl': 0.0, 'unit_price_excl': 0.0,
             'vat_percentage': 0.0, 'vat_amount': 0.0, 'type': 'product'},
        ]
        result = KlarnaPaymentMethod._format_klarna_articles(generic)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['articleNumber'], 'PAID')


class TestKlarnaPhoneAndHouseNumberSanitizers(BaseCase):

    def test_phone_strips_plus_and_whitespace(self):
        self.assertEqual(KlarnaPaymentMethod._sanitize_phone('+31 20 123 4567'), '31201234567')
        self.assertEqual(KlarnaPaymentMethod._sanitize_phone('+31612345678'), '31612345678')
        self.assertEqual(KlarnaPaymentMethod._sanitize_phone(''), '')
        self.assertEqual(KlarnaPaymentMethod._sanitize_phone(None), '')

    def test_house_number_splits_suffix(self):
        self.assertEqual(KlarnaPaymentMethod._split_house_number('1'), ('1', ''))
        self.assertEqual(KlarnaPaymentMethod._split_house_number('1A'), ('1', 'A'))
        self.assertEqual(KlarnaPaymentMethod._split_house_number('424'), ('424', ''))
        self.assertEqual(KlarnaPaymentMethod._split_house_number(''), ('', ''))


class TestKlarnaIdempotentResponseNormalization(BaseCase):
    """Klarna's "InvalidOrderStatus: Current status: Captured/Cancelled" 490
    response is treated as success — Odoo's serialization-failure retry can
    re-fire the SDK call after the first one already moved Klarna's order to
    the target state."""

    @staticmethod
    def _response(code, description=''):
        from buckaroo.models.payment_response import PaymentResponse
        return PaymentResponse({
            'data': {
                'Status': {
                    'Code': {'Code': code, 'Description': 'Failed'},
                    'SubCode': {'Code': 'S996', 'Description': description},
                    'DateTime': '2026-04-28T09:00:00',
                },
            },
        })

    def test_already_captured_490_normalizes_to_190(self):
        msg = ('An error occurred while processing the transaction: BadRequest: '
               'Error OrderService_Capture_InvalidOrderStatus: Order with Id X '
               'is not in a valid status to perform a capture. Current status: Captured.')
        response = self._response(490, msg)
        normalized = KlarnaPaymentMethod._buckaroo_klarna_normalize_idempotent(
            response, 'Captured',
        )
        self.assertEqual(normalized.status.code.code, 190)

    def test_already_cancelled_490_normalizes_to_190(self):
        msg = ('Error OrderService_Cancel_InvalidOrderStatus: Order with Id X '
               'is not in a valid status. Current status: Cancelled.')
        response = self._response(490, msg)
        normalized = KlarnaPaymentMethod._buckaroo_klarna_normalize_idempotent(
            response, ('Cancelled', 'Canceled'),
        )
        self.assertEqual(normalized.status.code.code, 190)

    def test_already_canceled_us_spelling_490_normalizes_to_190(self):
        """Klarna casing varies by region — accept both spellings for void."""
        msg = 'InvalidOrderStatus: Current status: Canceled.'
        response = self._response(490, msg)
        normalized = KlarnaPaymentMethod._buckaroo_klarna_normalize_idempotent(
            response, ('Cancelled', 'Canceled'),
        )
        self.assertEqual(normalized.status.code.code, 190)

    def test_unrelated_490_stays_490(self):
        """Real failures (not idempotency artifacts) must NOT be promoted."""
        response = self._response(490, 'BadRequest: Some unrelated Klarna error')
        normalized = KlarnaPaymentMethod._buckaroo_klarna_normalize_idempotent(
            response, 'Captured',
        )
        self.assertEqual(normalized.status.code.code, 490)

    def test_target_mismatch_stays_490(self):
        """Capture path must NOT swallow a Cancelled-status 490, and vice versa."""
        msg = 'InvalidOrderStatus: Current status: Cancelled.'
        response = self._response(490, msg)
        normalized = KlarnaPaymentMethod._buckaroo_klarna_normalize_idempotent(
            response, 'Captured',
        )
        self.assertEqual(normalized.status.code.code, 490)

    def test_success_response_passes_through(self):
        response = self._response(190, 'OK')
        normalized = KlarnaPaymentMethod._buckaroo_klarna_normalize_idempotent(
            response, 'Captured',
        )
        self.assertEqual(normalized.status.code.code, 190)
