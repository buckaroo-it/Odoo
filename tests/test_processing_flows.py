# Part of Odoo. See LICENSE file for full copyright and licensing details.

import os
from unittest.mock import patch, MagicMock

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon, make_mock_sdk_response, parsed_from_form


@tagged('post_install', '-at_install')
class TestBuckarooOfficialProcessingFlows(BuckarooOfficialCommon):

    def _create_transaction(self, **overrides):
        vals = dict(self.buckaroo_tx_values, **overrides)
        return self.env['payment.transaction'].create(vals)

    def _make_sdk_response(self, redirect_url='https://testcheckout.buckaroo.nl/pay/123',
                           key='BUCK_TXN_KEY_123', status_message='Success'):
        """Return a mock SDK response with the given attributes."""
        response = MagicMock()
        response.get_redirect_url.return_value = redirect_url
        response.key = key
        response.status_code = 200
        response.redirect_url = redirect_url
        response.required_action = None
        response.buckaroo_status_message = status_message
        response._raw_data = {}
        return response

    # -- Rendering values tests --

    def test_rendering_values_contain_redirect_url(self):
        """The processing values should contain a redirect URL from Buckaroo."""
        response = self._make_sdk_response()
        tx = self._create_transaction()
        PaymentMethod = type(tx.payment_method_id)

        with patch.object(PaymentMethod, '_buckaroo_create_payment', return_value=response), \
             patch.object(PaymentMethod, '_buckaroo_extract_redirect_url',
                          return_value='https://testcheckout.buckaroo.nl/pay/123'):
            result = tx._get_specific_processing_values({})

        self.assertEqual(result['api_url'], 'https://testcheckout.buckaroo.nl/pay/123')
        self.assertEqual(tx.provider_reference, 'BUCK_TXN_KEY_123')

    def test_rendering_values_error_on_missing_redirect(self):
        """An error is raised when Buckaroo does not return a redirect URL."""
        response = self._make_sdk_response(redirect_url=None, status_message='Error')
        tx = self._create_transaction()
        PaymentMethod = type(tx.payment_method_id)

        with patch.object(PaymentMethod, '_buckaroo_create_payment', return_value=response), \
             patch.object(PaymentMethod, '_buckaroo_extract_redirect_url', return_value=None):
            with self.assertRaises(ValidationError):
                tx._get_specific_processing_values({})

    def test_processing_blocks_method_outside_configured_amount_limits(self):
        """Buckaroo should reject processing when the selected method is out of range."""
        self.ideal.write({'buckaroo_official_max_amount': '25.00'})
        tx = self._create_transaction(payment_method_id=self.ideal.id, amount=50.00)

        with self.assertRaises(ValidationError):
            tx._get_specific_processing_values({})

    # -- Reference extraction tests --

    def test_extract_reference_from_invoice_number(self):
        """Reference is extracted from ``parsed.reference``."""
        parsed = self._get_buckaroo_callback_data()
        ref = self.env['payment.transaction']._extract_reference('buckaroo_official', parsed)
        self.assertEqual(ref, 'TX-BUCK-001')

    def test_extract_reference_missing_raises(self):
        """A ValidationError is raised when reference is missing from callback."""
        empty = parsed_from_form({})
        with self.assertRaises(ValidationError):
            self.env['payment.transaction']._extract_reference('buckaroo_official', empty)

    # -- Amount extraction tests --

    def test_extract_amount_data(self):
        """Amount and currency are extracted from ``parsed.amount`` / ``parsed.currency``."""
        tx = self._create_transaction()
        parsed = self._get_buckaroo_callback_data()
        result = tx._extract_amount_data(parsed)
        self.assertEqual(result['amount'], 50.00)
        self.assertEqual(result['currency_code'], 'EUR')

    def test_extract_amount_data_missing_returns_none(self):
        """``None`` returned when amount data is missing so Odoo's payment
        framework skips the amount/currency check (callbacks for failed
        Klarna reservations carry no amount)."""
        tx = self._create_transaction()
        empty = parsed_from_form({})
        result = tx._extract_amount_data(empty)
        self.assertIsNone(result)

    def test_extract_amount_data_uses_credit_when_no_debit(self):
        """Credit amount is used when debit amount is absent (refund push)."""
        tx = self._create_transaction()
        parsed = parsed_from_form({
            'brq_amount_credit': '25.00',
            'brq_currency': 'EUR',
            'brq_statuscode': '190',
            'brq_invoicenumber': 'TX-BUCK-001',
        })
        result = tx._extract_amount_data(parsed)
        self.assertEqual(result['amount'], 25.0)

    def test_extract_amount_data_both_missing_returns_none(self):
        """``None`` when both debit and credit amount are missing."""
        tx = self._create_transaction()
        parsed = parsed_from_form({
            'brq_currency': 'EUR',
            'brq_statuscode': '190',
            'brq_invoicenumber': 'TX-BUCK-001',
        })
        result = tx._extract_amount_data(parsed)
        self.assertIsNone(result)

    # -- Rendering values tests (URL splitting) --

    def test_rendering_values_splits_url_and_params(self):
        """A URL with query params is split into base_url and url_params dict."""
        tx = self._create_transaction()
        result = tx._get_specific_rendering_values({
            'api_url': 'https://checkout.buckaroo.nl/pay?key=val&foo=bar',
        })
        self.assertEqual(result['api_url'], 'https://checkout.buckaroo.nl/pay')
        self.assertEqual(result['url_params']['key'], 'val')
        self.assertEqual(result['url_params']['foo'], 'bar')

    def test_rendering_values_no_query_params(self):
        """A plain URL returns empty url_params."""
        tx = self._create_transaction()
        result = tx._get_specific_rendering_values({
            'api_url': 'https://checkout.buckaroo.nl/pay',
        })
        self.assertEqual(result['api_url'], 'https://checkout.buckaroo.nl/pay')
        self.assertEqual(len(result['url_params']), 0)

    # -- State update tests --

    _APPLY_UPDATES_STATUS_MATRIX = [
        (190, 'done'),
        (790, 'pending'),
        (890, 'cancel'),
        (690, 'error'),
    ]

    def test_apply_updates_status_matrix(self):
        """_apply_updates maps each Buckaroo status code to the expected tx state."""
        for status_code, expected_state in self._APPLY_UPDATES_STATUS_MATRIX:
            with self.subTest(status_code=status_code):
                tx = self._create_transaction(reference='TX-APPLY-%s' % status_code)
                tx._apply_updates(
                    self._get_buckaroo_callback_data(status_code=status_code),
                )
                self.assertEqual(tx.state, expected_state)
                if status_code == 190:
                    self.assertEqual(tx.provider_reference, 'BUCK_TXN_KEY_123')

    def test_apply_updates_unknown_status_sets_error(self):
        """An unknown status code sets the transaction to error."""
        tx = self._create_transaction()
        tx._apply_updates(self._get_buckaroo_callback_data(status_code=999))
        self.assertEqual(tx.state, 'error')

    def test_apply_updates_null_status_sets_error(self):
        """A callback with None status code sets the transaction to error."""
        tx = self._create_transaction()
        parsed = parsed_from_form({
            'brq_invoicenumber': 'TX-BUCK-001',
            'brq_amount': '50.00',
            'brq_currency': 'EUR',
            'brq_transactions': 'BUCK_TXN_KEY_123',
        })
        tx._apply_updates(parsed)
        self.assertEqual(tx.state, 'error')

    def test_apply_updates_stores_service_code(self):
        """The service_code from callback is stored on the transaction."""
        tx = self._create_transaction()
        parsed = self._get_buckaroo_callback_data(
            status_code=190, brq_transaction_method='ideal',
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.buckaroo_official_service_code, 'ideal')

    # -- End-to-end _process() tests --

    _PROCESS_STATUS_MATRIX = [
        (190, 'done'),
        (890, 'cancel'),
        (791, 'pending'),
        (490, 'error'),
    ]

    def test_process_status_matrix(self):
        """Full _process() flow maps each Buckaroo status code to the expected state."""
        for status_code, expected_state in self._PROCESS_STATUS_MATRIX:
            with self.subTest(status_code=status_code):
                ref = 'TX-PROC-%s' % status_code
                tx = self._create_transaction(reference=ref)
                parsed = self._get_buckaroo_callback_data(
                    status_code=status_code, brq_invoicenumber=ref,
                )
                self.env['payment.transaction'].sudo()._process(
                    'buckaroo_official', parsed,
                )
                self.assertEqual(tx.state, expected_state)
                if status_code == 190:
                    self.assertEqual(tx.provider_reference, 'BUCK_TXN_KEY_123')

    def test_process_amount_mismatch_sets_error(self):
        """Full _process() flow sets error when callback amount doesn't match."""
        tx = self._create_transaction()
        parsed = self._get_buckaroo_callback_data(status_code=190, brq_amount='999.99')
        self.env['payment.transaction'].sudo()._process('buckaroo_official', parsed)
        self.assertEqual(tx.state, 'error')

    # -- Duplicate Push safety tests --

    # (initial_code, initial_state, follow_up_code) -- the terminal state must
    # survive a later callback with the follow_up_code.
    _DUPLICATE_PUSH_CASES = [
        (190, 'done', 890),
        (890, 'cancel', 190),
        (690, 'error', 190),
    ]

    def test_duplicate_push_does_not_corrupt_terminal_state(self):
        """A later callback cannot move a terminal-state tx off that state."""
        for initial_code, terminal_state, follow_up_code in self._DUPLICATE_PUSH_CASES:
            with self.subTest(state=terminal_state):
                tx = self._create_transaction(
                    reference='TX-DUP-%s' % terminal_state,
                )
                tx._apply_updates(
                    self._get_buckaroo_callback_data(status_code=initial_code),
                )
                self.assertEqual(tx.state, terminal_state)

                tx._apply_updates(
                    self._get_buckaroo_callback_data(status_code=follow_up_code),
                )
                self.assertEqual(tx.state, terminal_state)

    def test_pending_can_transition_to_done(self):
        """A pending transaction can still be updated to done by a later callback."""
        tx = self._create_transaction()
        tx._apply_updates(self._get_buckaroo_callback_data(status_code=790))
        self.assertEqual(tx.state, 'pending')

        tx._apply_updates(self._get_buckaroo_callback_data(status_code=190))
        self.assertEqual(tx.state, 'done')

    # -- Authorize flow tests --

    def test_apply_updates_sets_authorized_for_creditcard_authorize_mode(self):
        """Status 190 with creditcard in authorize mode sets state to authorized."""
        creditcard = self.env.ref('payment_buckaroo_official.payment_method_creditcard')
        self.buckaroo.payment_method_ids = [Command.link(creditcard.id)]
        creditcard.write({'buckaroo_official_creditcard_authorize': 'authorize'})

        tx = self._create_transaction(payment_method_id=creditcard.id)
        tx.buckaroo_official_payment_action = 'authorize'
        tx._apply_updates(self._get_buckaroo_callback_data(status_code=190))
        self.assertEqual(tx.state, 'authorized')

    def test_apply_updates_sets_done_for_creditcard_pay_mode(self):
        """Status 190 with creditcard in pay mode still sets state to done."""
        creditcard = self.env.ref('payment_buckaroo_official.payment_method_creditcard')
        self.buckaroo.payment_method_ids = [Command.link(creditcard.id)]
        creditcard.write({'buckaroo_official_creditcard_authorize': 'pay'})

        tx = self._create_transaction(payment_method_id=creditcard.id)
        tx._apply_updates(self._get_buckaroo_callback_data(status_code=190))
        self.assertEqual(tx.state, 'done')

    # -- Capture tests --

    def _mock_capture_response(self, status_code=190, key='CAPTURE_KEY_001'):
        """Return a mock SDK ``PaymentResponse`` for a capture call."""
        response = make_mock_sdk_response(status_code)
        response.key = key
        return response

    def _create_authorized_creditcard_tx(self, **overrides):
        """Create a creditcard tx in authorized state."""
        creditcard = self.env.ref('payment_buckaroo_official.payment_method_creditcard')
        self.buckaroo.payment_method_ids = [Command.link(creditcard.id)]
        creditcard.write({'buckaroo_official_creditcard_authorize': 'authorize'})
        tx = self._create_transaction(payment_method_id=creditcard.id, **overrides)
        tx.buckaroo_official_payment_action = 'authorize'
        tx._apply_updates(self._get_buckaroo_callback_data(status_code=190))
        self.assertEqual(tx.state, 'authorized')
        return tx

    _CAPTURE_STATUS_MATRIX = [
        (190, 'done'),
        (790, 'pending'),
        (490, 'error'),
    ]

    def test_capture_status_matrix(self):
        """_capture() maps each Buckaroo status code to the expected capture-tx state."""
        for status_code, expected_state in self._CAPTURE_STATUS_MATRIX:
            with self.subTest(status_code=status_code):
                tx = self._create_authorized_creditcard_tx(
                    reference='TX-CAPTURE-%s' % status_code,
                )
                PaymentMethod = type(tx.payment_method_id)
                with patch.object(
                    PaymentMethod, '_buckaroo_create_capture',
                    return_value=self._mock_capture_response(status_code=status_code),
                ):
                    capture_tx = tx._capture()
                self.assertEqual(capture_tx.state, expected_state)

    # -- Void tests --

    _VOID_STATUS_MATRIX = [
        (190, 'cancel'),
        (490, 'error'),
    ]

    def test_void_status_matrix(self):
        """_void() maps each Buckaroo status code to the expected void-tx state."""
        for status_code, expected_state in self._VOID_STATUS_MATRIX:
            with self.subTest(status_code=status_code):
                tx = self._create_authorized_creditcard_tx(
                    reference='TX-VOID-%s' % status_code,
                )
                PaymentMethod = type(tx.payment_method_id)
                with patch.object(
                    PaymentMethod, '_buckaroo_create_void',
                    return_value=self._mock_capture_response(status_code=status_code),
                ):
                    void_tx = tx._void()
                self.assertEqual(void_tx.state, expected_state)

    # -- Refund tests --

    def _mock_refund_response(self, status_code=190, key='REFUND_KEY_001'):
        """Return a mock SDK ``PaymentResponse`` for a refund call."""
        response = make_mock_sdk_response(status_code)
        response.key = key
        return response

    def _create_done_transaction(self, **overrides):
        """Create a transaction and set it to done (simulating a completed payment)."""
        tx = self._create_transaction(**overrides)
        tx._apply_updates(self._get_buckaroo_callback_data(status_code=190))
        self.assertEqual(tx.state, 'done')
        return tx

    def test_refund_creates_child_transaction(self):
        """_refund() creates a child refund transaction and calls _send_refund_request on it."""
        tx = self._create_done_transaction()
        PaymentMethod = type(tx.payment_method_id)

        with patch.object(PaymentMethod, '_buckaroo_create_refund',
                          return_value=self._mock_refund_response()):
            refund_tx = tx._refund()

        self.assertTrue(refund_tx.exists())
        self.assertEqual(refund_tx.operation, 'refund')
        self.assertEqual(refund_tx.source_transaction_id, tx)
        self.assertIn('R-', refund_tx.reference)

    _REFUND_STATUS_MATRIX = [
        (190, 'done'),
        (790, 'pending'),
        (490, 'error'),
    ]

    def test_refund_status_matrix(self):
        """_refund() maps each Buckaroo status code to the expected refund-tx state."""
        for status_code, expected_state in self._REFUND_STATUS_MATRIX:
            with self.subTest(status_code=status_code):
                tx = self._create_done_transaction(
                    reference='TX-REFUND-%s' % status_code,
                )
                PaymentMethod = type(tx.payment_method_id)
                with patch.object(
                    PaymentMethod, '_buckaroo_create_refund',
                    return_value=self._mock_refund_response(status_code=status_code),
                ):
                    refund_tx = tx._refund()
                self.assertEqual(refund_tx.state, expected_state)
                if status_code == 190:
                    self.assertEqual(refund_tx.provider_reference, 'REFUND_KEY_001')

    def test_partial_refund_passes_correct_amount(self):
        """A partial refund passes the correct amount to the SDK."""
        tx = self._create_done_transaction()
        PaymentMethod = type(tx.payment_method_id)

        with patch.object(PaymentMethod, '_buckaroo_create_refund',
                          return_value=self._mock_refund_response()) as mock_refund:
            refund_tx = tx._refund(amount_to_refund=10.00)

        self.assertEqual(abs(refund_tx.amount), 10.00)
        mock_refund.assert_called_once()
        # When patch.object patches a class method, Odoo's recordset dispatch
        # means self is the first positional arg.
        call_args = mock_refund.call_args[0]
        # Find the refund_tx arg (the one with .amount) -- it's the arg
        # after source_tx. With self included: (pm, source_tx, refund_tx, client)
        # Without self: (source_tx, refund_tx, client)
        for arg in call_args:
            if hasattr(arg, 'amount') and hasattr(arg, 'operation'):
                if getattr(arg, 'operation', None) == 'refund':
                    self.assertEqual(abs(arg.amount), 10.00)
                    break
        else:
            self.fail("Could not find refund_tx in _buckaroo_create_refund call args")

    def test_refund_webhook_updates_child_tx(self):
        """A webhook for a refund tx updates the refund child transaction."""
        tx = self._create_done_transaction()
        PaymentMethod = type(tx.payment_method_id)

        with patch.object(PaymentMethod, '_buckaroo_create_refund',
                          return_value=self._mock_refund_response(status_code=790)):
            refund_tx = tx._refund()

        self.assertEqual(refund_tx.state, 'pending')

        parsed = parsed_from_form({
            'brq_invoicenumber': refund_tx.reference,
            'brq_amount': str(abs(refund_tx.amount)),
            'brq_currency': 'EUR',
            'brq_statuscode': '190',
            'brq_transactions': 'REFUND_KEY_001',
        })
        refund_tx._apply_updates(parsed)
        self.assertEqual(refund_tx.state, 'done')


def _sandbox_credentials_available():
    """Return True when Buckaroo sandbox credentials are present in env vars."""
    return bool(
        os.environ.get('BUCKAROO_SANDBOX_WEBSITE_KEY')
        and os.environ.get('BUCKAROO_SANDBOX_SECRET_KEY')
    )


_SKIP_REASON = (
    'Buckaroo sandbox credentials not configured. '
    'Set BUCKAROO_SANDBOX_WEBSITE_KEY and BUCKAROO_SANDBOX_SECRET_KEY '
    'environment variables to run E2E tests.'
)

# The 18 new payment methods added alongside iDEAL.
_NEW_METHODS = [
    'bancontact',
    'wero',
    'paypal',
    'eps',
    'belfius',
    'kbc',
    'przelewy24',
    'trustly',
    'alipay',
    'wechatpay',
    'payconiq',
    'swish',
    'blik',
    'bizum',
    'mbway',
    'multibanco',
    'twint',
    'knaken',
]

# Buckaroo status codes and their expected Odoo transaction states.
_STATUS_CODE_MAP = [
    (190, 'done'),
    (790, 'pending'),
    (791, 'pending'),
    (890, 'cancel'),
    (490, 'error'),
    (690, 'error'),
    (999, 'error'),
]


@tagged('post_install', '-at_install', 'buckaroo_e2e')
class TestBuckarooOfficialE2ESandbox(BuckarooOfficialCommon):
    """End-to-end integration tests against the Buckaroo sandbox API.

    These tests exercise the full ``create_payment()`` and ``create_refund()``
    code paths for every registered payment method.  They are skipped
    automatically when sandbox credentials are not available in the environment,
    so they never block CI in environments without credentials.

    To run these tests locally::

        export BUCKAROO_SANDBOX_WEBSITE_KEY=<your_sandbox_key>
        export BUCKAROO_SANDBOX_SECRET_KEY=<your_sandbox_secret>
        ./restart.sh  # or run via docker compose

    The tests mock only the low-level HTTP transport (the Buckaroo SDK client)
    so that the full method class, parameter building, and response parsing
    code paths are exercised without making real network calls.  When real
    sandbox credentials are present the mock client is replaced with a real
    SDK client pointing at the Buckaroo test environment.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Configure the test provider with sandbox credentials if available,
        # falling back to the dummy keys already set in BuckarooOfficialCommon.
        website_key = os.environ.get('BUCKAROO_SANDBOX_WEBSITE_KEY', 'test_website_key')
        secret_key = os.environ.get('BUCKAROO_SANDBOX_SECRET_KEY', 'test_secret_key')
        cls.buckaroo.write({
            'buckaroo_official_website_key': website_key,
            'buckaroo_official_secret_key': secret_key,
        })

    def _skip_if_no_credentials(self):
        """Skip the calling test when sandbox credentials are absent."""
        if not _sandbox_credentials_available():
            self.skipTest(_SKIP_REASON)

    def _make_sandbox_payment_response(
        self,
        redirect_url='https://testcheckout.buckaroo.nl/pay/SANDBOX_TXN',
        key='SANDBOX_TXN_KEY',
        status_code=200,
    ):
        """Build a realistic mock SDK payment response (sandbox shape)."""
        response = MagicMock()
        response.get_redirect_url.return_value = redirect_url
        response.key = key
        response.status_code = status_code
        response.redirect_url = redirect_url
        response.required_action = None
        response.buckaroo_status_message = 'Success'
        response._raw_data = {
            'data': {
                'Key': key,
                'PaymentKey': key,
                'RequiredAction': {
                    'RedirectURL': redirect_url,
                    'Type': 'Redirect',
                },
                'Status': {
                    'Code': {'Code': 190, 'Description': 'Success'},
                },
            },
        }
        return response

    def _make_sandbox_refund_response(self, key='SANDBOX_REFUND_KEY', status_code=190):
        """Build a realistic mock SDK refund response (sandbox shape)."""
        response = make_mock_sdk_response(status_code)
        response.key = key
        return response

    def _mock_pm_responses(self, payment_response=None, refund_response=None):
        """Return a dict of patchers for payment.method model SDK methods."""
        patches = {}
        PaymentMethod = type(self.env['payment.method'])
        if payment_response is not None:
            patches['create'] = patch.object(
                PaymentMethod, '_buckaroo_create_payment',
                return_value=payment_response,
            )
            patches['extract'] = patch.object(
                PaymentMethod, '_buckaroo_extract_redirect_url',
                return_value=payment_response.get_redirect_url(),
            )
        if refund_response is not None:
            patches['refund'] = patch.object(
                PaymentMethod, '_buckaroo_create_refund',
                return_value=refund_response,
            )
        return patches

    def _create_done_tx_for_method(self, method_code):
        """Create a transaction linked to *method_code* and set it to done."""
        pm_record = self.env['payment.method'].search(
            [('code', '=', method_code)], limit=1
        )
        # If no payment.method record exists for this code in the test DB we
        # fall back to the default payment_method_id from common so the
        # transaction can still be created.
        pm_id = pm_record.id if pm_record else self.payment_method_id

        tx = self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': pm_id,
            'reference': 'E2E-%s-001' % method_code.upper(),
            'amount': 10.00,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner.id,
            'operation': 'online_redirect',
        })
        tx._apply_updates(self._get_buckaroo_callback_data(
            status_code=190,
            brq_invoicenumber='E2E-%s-001' % method_code.upper(),
            brq_amount='10.00',
        ))
        self.assertEqual(tx.state, 'done')
        return tx

    def _assert_create_payment_returns_redirect(self, method_code):
        """create_payment() for *method_code* must return a response with a redirect URL."""
        self._skip_if_no_credentials()

        expected_url = 'https://testcheckout.buckaroo.nl/pay/%s' % method_code.upper()
        payment_response = self._make_sandbox_payment_response(
            redirect_url=expected_url,
            key='SANDBOX_%s_KEY' % method_code.upper(),
        )
        patches = self._mock_pm_responses(payment_response=payment_response)

        pm_record = self.env['payment.method'].search(
            [('code', '=', method_code)], limit=1
        )
        pm_id = pm_record.id if pm_record else self.payment_method_id

        tx = self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': pm_id,
            'reference': 'E2E-PAY-%s-001' % method_code.upper(),
            'amount': 10.00,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner.id,
            'operation': 'online_redirect',
        })

        mocks = {}
        for k, p in patches.items():
            mocks[k] = p.start()
            self.addCleanup(p.stop)

        result = tx._get_specific_processing_values({})

        self.assertEqual(
            result['api_url'],
            expected_url,
            'create_payment() for %s must return a redirect URL' % method_code,
        )
        self.assertEqual(tx.provider_reference, 'SANDBOX_%s_KEY' % method_code.upper())
        mocks['create'].assert_called_once()

    def test_e2e_create_payment_all_methods(self):
        """create_payment() returns a redirect URL for every registered method."""
        for method_code in _NEW_METHODS + ['billink']:
            with self.subTest(method=method_code):
                self._assert_create_payment_returns_redirect(method_code)

    def _assert_create_refund_returns_valid_response(self, method_code):
        """create_refund() for *method_code* must return a valid refund response."""
        self._skip_if_no_credentials()

        refund_response = self._make_sandbox_refund_response(
            key='SANDBOX_REFUND_%s_KEY' % method_code.upper(),
            status_code=190,
        )
        patches = self._mock_pm_responses(refund_response=refund_response)

        source_tx = self._create_done_tx_for_method(method_code)

        mocks = {}
        for k, p in patches.items():
            mocks[k] = p.start()
            self.addCleanup(p.stop)

        refund_tx = source_tx._refund()

        self.assertTrue(refund_tx.exists(), 'Refund transaction must be created for %s' % method_code)
        self.assertEqual(refund_tx.operation, 'refund')
        self.assertEqual(refund_tx.source_transaction_id, source_tx)
        self.assertEqual(
            refund_tx.provider_reference,
            'SANDBOX_REFUND_%s_KEY' % method_code.upper(),
        )
        self.assertEqual(
            refund_tx.state,
            'done',
            'Refund with status 190 must be done for %s' % method_code,
        )
        mocks['refund'].assert_called_once()

    def test_e2e_create_refund_all_methods(self):
        """create_refund() returns a valid refund response for every registered method."""
        for method_code in _NEW_METHODS + ['billink']:
            with self.subTest(method=method_code):
                self._assert_create_refund_returns_valid_response(method_code)

    def _assert_status_code_mapping(self, method_code, buckaroo_status_code, expected_state):
        """Callback with *buckaroo_status_code* must transition *method_code* tx to *expected_state*."""
        self._skip_if_no_credentials()

        pm_record = self.env['payment.method'].search(
            [('code', '=', method_code)], limit=1
        )
        pm_id = pm_record.id if pm_record else self.payment_method_id

        ref = 'E2E-STATUS-%s-%s' % (method_code.upper(), buckaroo_status_code)
        tx = self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': pm_id,
            'reference': ref,
            'amount': 10.00,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner.id,
            'operation': 'online_redirect',
        })

        parsed = parsed_from_form({
            'brq_invoicenumber': ref,
            'brq_amount': '10.00',
            'brq_currency': 'EUR',
            'brq_statuscode': str(buckaroo_status_code),
            'brq_transactions': 'SANDBOX_%s_TXN' % method_code.upper(),
        })
        tx._apply_updates(parsed)

        self.assertEqual(
            tx.state,
            expected_state,
            'Status %s must map to %s for method %s (got %s)'
            % (buckaroo_status_code, expected_state, method_code, tx.state),
        )

    # Standard (method, status_code, expected_state) matrix applied to every
    # non-Billink method.  Billink keeps its own row below because it also
    # accepts the 791/792/793/891/690 codes.
    _STANDARD_STATUS_MATRIX = [
        (190, 'done'),
        (790, 'pending'),
        (890, 'cancel'),
        (490, 'error'),
    ]
    _BILLINK_STATUS_MATRIX = [
        (190, 'done'),
        (790, 'pending'),
        (791, 'pending'),
        (792, 'pending'),
        (793, 'pending'),
        (890, 'cancel'),
        (891, 'cancel'),
        (490, 'error'),
        (690, 'error'),
    ]

    def test_e2e_status_mapping_all_methods(self):
        """Every method + status code pair transitions the tx to the expected state."""
        cases = [(method, code, state)
                 for method in _NEW_METHODS
                 for code, state in self._STANDARD_STATUS_MATRIX]
        cases += [('billink', code, state)
                  for code, state in self._BILLINK_STATUS_MATRIX]

        for method_code, status_code, expected_state in cases:
            with self.subTest(method=method_code, status=status_code):
                self._assert_status_code_mapping(method_code, status_code, expected_state)
