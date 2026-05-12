# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""End-to-end + unit tests for the Bank Transfer payment method.

Covers payment creation (asserts customer service params are added before
``.pay()`` is called), webhook state transitions (form + JSON), refund
lifecycle, and bank_transfer-only dispatch on payment.method. SDK transport
is mocked throughout.
"""

from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import tagged

from .common import (
    BuckarooOfficialCommon,
    CALLBACK_CANCEL_CODES,
    CALLBACK_DONE_CODES,
    CALLBACK_ERROR_CODES,
    CALLBACK_PENDING_CODES,
    REFUND_STATUS_CASES,
    make_mock_sdk_builder,
    make_mock_sdk_response,
    parsed_from_form,
    parsed_from_json,
)


@tagged('post_install', '-at_install')
class TestBankTransferPaymentCreation(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bank_transfer = cls.env.ref(
            'payment_buckaroo_official.payment_method_bank_transfer'
        )
        cls.buckaroo.payment_method_ids = [Command.link(cls.bank_transfer.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan de Vries',
            'country_id': cls.env.ref('base.nl').id,
            'email': 'jan@example.nl',
        })

    def _sdk_response(self, redirect_url='https://testcheckout.buckaroo.nl/pay/BT',
                      key='BT_KEY'):
        response = MagicMock()
        response.get_redirect_url.return_value = redirect_url
        response.key = key
        response.status_code = 200
        response.redirect_url = redirect_url
        response.required_action = None
        response.buckaroo_status_message = 'Success'
        response._raw_data = {}
        return response

    def _create_tx(self, reference='BT-TX-001', amount=100.0):
        return self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.bank_transfer.id,
            'reference': reference,
            'amount': amount,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner_nl.id,
            'operation': 'online_redirect',
        })

    def test_payment_creation_returns_redirect_and_stores_reference(self):
        response = self._sdk_response(redirect_url='https://bt/pay/X', key='STORED_KEY')
        tx = self._create_tx()
        PaymentMethod = type(tx.payment_method_id)

        with patch.object(PaymentMethod, '_buckaroo_create_payment', return_value=response), \
             patch.object(PaymentMethod, '_buckaroo_extract_redirect_url',
                          return_value='https://bt/pay/X'):
            result = tx._get_specific_processing_values({})

        self.assertEqual(result['api_url'], 'https://bt/pay/X')
        self.assertEqual(tx.provider_reference, 'STORED_KEY')

    def test_payment_creation_missing_redirect_raises_when_not_pending(self):
        """Non-pending response without redirect → ValidationError (failure path)."""
        response = self._sdk_response(redirect_url=None)
        response.is_pending = MagicMock(return_value=False)
        response.get_some_error = MagicMock(return_value='Some failure')
        tx = self._create_tx()
        PaymentMethod = type(tx.payment_method_id)

        with patch.object(PaymentMethod, '_buckaroo_create_payment', return_value=response), \
             patch.object(PaymentMethod, '_buckaroo_extract_redirect_url', return_value=None):
            with self.assertRaises(ValidationError):
                tx._get_specific_processing_values({})

    def test_payment_creation_pending_without_redirect_routes_to_validate_then_confirmation(self):
        """Buckaroo returns success-pending for Bank Transfer without an
        external redirect URL — customer must be routed via
        ``/shop/payment/validate`` so they land on the order confirmation
        page (with bank details rendered inline), not the
        ``/payment/status`` "Please wait..." page that's wrong UX for an
        offline transfer."""
        response = self._sdk_response(redirect_url=None, key='BT_PENDING_KEY')
        response.is_pending = MagicMock(return_value=True)
        tx = self._create_tx()
        PaymentMethod = type(tx.payment_method_id)

        with patch.object(PaymentMethod, '_buckaroo_create_payment', return_value=response), \
             patch.object(PaymentMethod, '_buckaroo_extract_redirect_url', return_value=None):
            result = tx._get_specific_processing_values({})

        self.assertTrue(result['api_url'].endswith('/shop/payment/validate'))
        self.assertEqual(tx.provider_reference, 'BT_PENDING_KEY')
        self.assertEqual(tx.state, 'pending')


@tagged('post_install', '-at_install')
class TestBankTransferWebhookCallbacks(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bank_transfer = cls.env.ref(
            'payment_buckaroo_official.payment_method_bank_transfer'
        )
        cls.buckaroo.payment_method_ids = [Command.link(cls.bank_transfer.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan de Vries',
            'country_id': cls.env.ref('base.nl').id,
        })

    def _create_tx(self, reference, amount=100.0):
        return self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.bank_transfer.id,
            'reference': reference,
            'amount': amount,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner_nl.id,
            'operation': 'online_redirect',
        })

    def _form_callback(self, reference, status_code, txn_key='BT_TXN_001'):
        return parsed_from_form({
            'brq_invoicenumber': reference,
            'brq_amount': '100.00',
            'brq_currency': 'EUR',
            'brq_statuscode': str(status_code),
            'brq_transactions': txn_key,
        })

    def _json_callback(self, reference, status_code, txn_key='BT_TXN_001'):
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
                    ref = 'BT-WH-%s-%s' % (fmt.upper(), code)
                    tx = self._create_tx(ref)
                    builder = self._form_callback if fmt == 'form' else self._json_callback
                    parsed = builder(ref, code)
                    self.env['payment.transaction'].sudo()._process('buckaroo_official', parsed)
                    self.assertEqual(tx.state, expected_state)

    def test_done_status_maps_to_done(self):
        self._assert_status_maps_to_state(CALLBACK_DONE_CODES, 'done')
        tx = self._create_tx('BT-WH-REF-190')
        parsed = self._form_callback('BT-WH-REF-190', 190)
        self.env['payment.transaction'].sudo()._process('buckaroo_official', parsed)
        self.assertEqual(tx.provider_reference, 'BT_TXN_001')

    def test_pending_status_maps_to_pending(self):
        self._assert_status_maps_to_state(CALLBACK_PENDING_CODES, 'pending')

    def test_cancel_status_maps_to_cancel(self):
        self._assert_status_maps_to_state(CALLBACK_CANCEL_CODES, 'cancel')

    def test_error_status_maps_to_error(self):
        self._assert_status_maps_to_state(CALLBACK_ERROR_CODES, 'error')


@tagged('post_install', '-at_install')
class TestBankTransferRefundFlow(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bank_transfer = cls.env.ref(
            'payment_buckaroo_official.payment_method_bank_transfer'
        )
        cls.buckaroo.payment_method_ids = [Command.link(cls.bank_transfer.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan de Vries',
            'country_id': cls.env.ref('base.nl').id,
        })

    def _create_done_tx(self, reference='BT-DONE', amount=100.0):
        tx = self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.bank_transfer.id,
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
            'brq_transactions': 'BT_DONE_KEY',
        }))
        self.assertEqual(tx.state, 'done')
        return tx

    def _mock_refund_response(self, key='BT_REFUND_KEY', status_code=190):
        response = make_mock_sdk_response(status_code)
        response.key = key
        return response

    def _patch_refund(self, response):
        PaymentMethod = type(self.env['payment.method'])
        return patch.object(PaymentMethod, '_buckaroo_create_refund', return_value=response)

    def test_refund_creates_linked_child_tx(self):
        tx = self._create_done_tx(reference='BT-REFUND-CREATE')
        with self._patch_refund(self._mock_refund_response()):
            refund_tx = tx._refund()

        self.assertEqual(refund_tx.operation, 'refund')
        self.assertEqual(refund_tx.source_transaction_id, tx)
        self.assertIn('R-', refund_tx.reference)

    def test_refund_status_matrix(self):
        for status_code, expected_state in REFUND_STATUS_CASES:
            with self.subTest(status_code=status_code, expected_state=expected_state):
                tx = self._create_done_tx(reference='BT-REFUND-%s' % status_code)
                with self._patch_refund(self._mock_refund_response(
                    key='BT_REFUND_%s' % status_code, status_code=status_code,
                )):
                    refund_tx = tx._refund()
                self.assertEqual(refund_tx.state, expected_state)

    def test_partial_refund_passes_amount_to_sdk(self):
        tx = self._create_done_tx(reference='BT-PARTIAL', amount=100.0)
        with self._patch_refund(self._mock_refund_response()) as mock_refund:
            refund_tx = tx._refund(amount_to_refund=25.0)

        self.assertEqual(abs(refund_tx.amount), 25.0)
        refund_tx_arg = mock_refund.call_args[0][1]
        self.assertEqual(abs(refund_tx_arg.amount), 25.0)


@tagged('post_install', '-at_install')
class TestBankTransferCreatePaymentDispatch(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bank_transfer = cls.env.ref(
            'payment_buckaroo_official.payment_method_bank_transfer'
        )
        cls.buckaroo.payment_method_ids = [Command.link(cls.bank_transfer.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan de Vries',
            'country_id': cls.env.ref('base.nl').id,
            'email': 'jan@example.nl',
        })

    def _create_tx(self, payment_method=None):
        return self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': (payment_method or self.bank_transfer).id,
            'reference': 'TX-BT-001',
            'amount': 50.0,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner_nl.id,
            'operation': 'online_redirect',
        })

    def test_non_bank_transfer_falls_through_to_super(self):
        tx = self._create_tx(payment_method=self.ideal)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        mock_builder.add_parameter.assert_not_called()
        self.assertEqual(result, mock_response)

    def test_bank_transfer_adds_required_customer_parameters(self):
        tx = self._create_tx()
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_bank_transfer.PaymentService'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.bank_transfer._buckaroo_create_payment(tx, client)

        params = {call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list}
        self.assertEqual(params['customeremail'], 'jan@example.nl')
        self.assertEqual(params['customerfirstname'], 'Jan')
        self.assertEqual(params['customerlastname'], 'de Vries')
        self.assertEqual(params['customerCountry'], 'NL')
        mock_builder.pay.assert_called_once()
        self.assertEqual(result, mock_response)

    def test_bank_transfer_omits_country_when_partner_has_none(self):
        partner_no_country = self.env['res.partner'].create({
            'name': 'No Country',
            'email': 'nc@example.com',
        })
        tx = self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.bank_transfer.id,
            'reference': 'TX-BT-NC',
            'amount': 50.0,
            'currency_id': self.currency_euro.id,
            'partner_id': partner_no_country.id,
            'operation': 'online_redirect',
        })
        client = MagicMock()
        mock_builder, _resp = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_bank_transfer.PaymentService'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.bank_transfer._buckaroo_create_payment(tx, client)

        param_names = [call[0][0] for call in mock_builder.add_parameter.call_args_list]
        self.assertNotIn('customerCountry', param_names)

    def test_bank_transfer_uses_sdk_service_name_transfer(self):
        """SDK factory key is ``transfer`` (lowercase); the data record sets
        ``buckaroo_official_sdk_service_name=transfer`` so ``code=bank_transfer``
        still routes to ``TransferBuilder``."""
        self.assertEqual(
            self.bank_transfer._buckaroo_get_sdk_service_name(),
            'transfer',
        )


@tagged('post_install', '-at_install')
class TestBankTransferDetailCapture(BuckarooOfficialCommon):
    """Bank transfer details (IBAN/BIC/AccountHolder/PaymentReference)
    must be captured from both the SDK Pay response and from incoming
    pushes (form-encoded ``brq_SERVICE_transfer_*`` and JSON
    ``Services[*].Parameters``) so the confirmation page can render them."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bank_transfer = cls.env.ref(
            'payment_buckaroo_official.payment_method_bank_transfer'
        )
        cls.buckaroo.payment_method_ids = [Command.link(cls.bank_transfer.id)]

    def _create_tx(self, reference='BT-DETAIL'):
        return self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.bank_transfer.id,
            'reference': reference,
            'amount': 50.0,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner.id,
            'operation': 'online_redirect',
        })

    def _make_sdk_response_with_services(self):
        params = {
            'IBAN': 'NL05RABO0121503038',
            'BIC': 'RABONL2U',
            'AccountHolderName': 'Buckaroo Stichting Derdengelden',
            'PaymentReference': '14256252',
        }
        response = MagicMock()
        response.get_redirect_url.return_value = None
        response.required_action = None
        response.key = 'BT_KEY'
        response.is_pending.return_value = True
        response.get_service_parameter.side_effect = (
            lambda name: next(
                (v for k, v in params.items() if k.lower() == name.lower()),
                None,
            )
        )
        return response

    def test_apply_updates_writes_bank_fields_from_json_push(self):
        tx = self._create_tx(reference='BT-WH-JSON')
        parsed = parsed_from_json({
            'Transaction': {
                'Invoice': 'BT-WH-JSON',
                'AmountDebit': 50.0,
                'Currency': 'EUR',
                'Key': 'BT_KEY_JSON',
                'Status': {'Code': {'Code': 792}},
                'Services': [{
                    'Name': 'transfer',
                    'Parameters': [
                        {'Name': 'IBAN', 'Value': 'NL05RABO0121503038'},
                        {'Name': 'BIC', 'Value': 'RABONL2U'},
                        {'Name': 'AccountHolderName', 'Value': 'Buckaroo'},
                        {'Name': 'PaymentReference', 'Value': '777'},
                    ],
                }],
            },
        })
        tx._apply_updates(parsed)
        self.assertEqual(tx.buckaroo_official_bank_iban, 'NL05RABO0121503038')
        self.assertEqual(tx.buckaroo_official_bank_bic, 'RABONL2U')
        self.assertEqual(tx.buckaroo_official_bank_account_holder, 'Buckaroo')
        self.assertEqual(tx.buckaroo_official_bank_payment_reference, '777')

    def test_no_redirect_handler_persists_bank_fields_on_tx(self):
        tx = self._create_tx()
        response = self._make_sdk_response_with_services()

        self.bank_transfer._buckaroo_handle_no_redirect_response(tx, response)

        self.assertEqual(tx.buckaroo_official_bank_iban, 'NL05RABO0121503038')
        self.assertEqual(tx.buckaroo_official_bank_bic, 'RABONL2U')
        self.assertEqual(
            tx.buckaroo_official_bank_account_holder,
            'Buckaroo Stichting Derdengelden',
        )
        self.assertEqual(tx.buckaroo_official_bank_payment_reference, '14256252')

    def test_apply_updates_writes_bank_fields_from_form_push(self):
        tx = self._create_tx(reference='BT-WH-DETAIL')
        parsed = parsed_from_form({
            'brq_invoicenumber': 'BT-WH-DETAIL',
            'brq_amount': '50.00',
            'brq_currency': 'EUR',
            'brq_statuscode': '792',
            'brq_transactions': 'BT_KEY',
            'brq_payment_method': 'transfer',
            'brq_transaction_method': 'transfer',
            'brq_SERVICE_transfer_IBAN': 'NL05RABO0121503038',
            'brq_SERVICE_transfer_PaymentReference': '14256252',
        })
        tx._apply_updates(parsed)
        self.assertEqual(tx.buckaroo_official_bank_iban, 'NL05RABO0121503038')
        self.assertEqual(tx.buckaroo_official_bank_payment_reference, '14256252')


@tagged('post_install', '-at_install')
class TestBankTransferCurrencyRestriction(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bank_transfer = cls.env.ref(
            'payment_buckaroo_official.payment_method_bank_transfer'
        )
        cls.buckaroo.payment_method_ids = [Command.link(cls.bank_transfer.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'NL', 'country_id': cls.env.ref('base.nl').id,
        })

    def _visible(self, currency=None):
        methods = self.env['payment.method']._get_compatible_payment_methods(
            self.buckaroo.ids,
            self.partner_nl.id,
            currency_id=(currency or self.currency_euro).id,
            amount=100.0,
        )
        return self.bank_transfer in methods

    def test_eur_visible(self):
        self.assertTrue(self._visible())

    def test_non_eur_currency_hidden(self):
        self.assertFalse(self._visible(currency=self.env.ref('base.USD')))
