# Part of Odoo. See LICENSE file for full copyright and licensing details.

import base64
from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon, make_mock_sdk_builder


class _patch_request:
    def __init__(self, session_dict):
        mock_req = MagicMock()
        mock_req.session = session_dict
        self._patch = patch('odoo.http.request', mock_req)

    def __enter__(self):
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


@tagged('post_install', '-at_install')
class TestGooglepayPaymentMethodRecord(BuckarooOfficialCommon):

    def test_googlepay_record_loads_with_expected_fields(self):
        method = self.env.ref('payment_buckaroo_official.payment_method_googlepay')

        self.assertEqual(method.code, 'googlepay')
        self.assertEqual(method.buckaroo_official_sdk_service_name, 'googlepay')
        self.assertEqual(method.support_refund, 'partial')
        self.assertTrue(method.active)
        self.assertEqual(
            set(method.supported_currency_ids.mapped('name')), {'EUR'},
        )


@tagged('post_install', '-at_install')
class TestGooglepayAdminFields(BuckarooOfficialCommon):

    def test_googlepay_admin_fields_persist(self):
        method = self.env.ref('payment_buckaroo_official.payment_method_googlepay')
        method.write({
            'buckaroo_official_googlepay_merchant_guid': 'BUCK-MERCHANT-GUID-123',
            'buckaroo_official_googlepay_google_merchant_id': 'GOOG-MERCHANT-456',
        })

        method.invalidate_recordset()
        self.assertEqual(
            method.buckaroo_official_googlepay_merchant_guid,
            'BUCK-MERCHANT-GUID-123',
        )
        self.assertEqual(
            method.buckaroo_official_googlepay_google_merchant_id,
            'GOOG-MERCHANT-456',
        )


@tagged('post_install', '-at_install')
class TestGooglepaySessionBridgeController(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.googlepay = cls.env.ref(
            'payment_buckaroo_official.payment_method_googlepay'
        )

    def _call_controller(self, payment_method_id=None, **kwargs):
        import odoo.http
        from ..controllers.googlepay import GooglepayPaymentPortal
        portal = GooglepayPaymentPortal()
        fake_req = MagicMock()
        fake_req.env = self.env
        fake_req.session = {}
        if payment_method_id is not None:
            kwargs['payment_method_id'] = payment_method_id
        with patch(
            'odoo.addons.website_sale.controllers.payment.PaymentPortal.shop_payment_transaction',
            return_value='SUPER_OK',
        ) as super_stub, patch.object(
            odoo.http, 'request', fake_req,
        ), patch(
            'odoo.addons.payment_buckaroo_official.controllers.googlepay.request',
            new=fake_req,
        ):
            result = portal.shop_payment_transaction(
                order_id=1, access_token='tok', **kwargs,
            )
            return result, super_stub, fake_req

    def test_token_and_customer_name_stashed_on_session_and_stripped_from_kwargs(self):
        result, super_stub, fake_req = self._call_controller(
            payment_method_id=self.googlepay.id,
            buckaroo_googlepay_token='gp-tok-abc',
            buckaroo_googlepay_customer_name='Ada Lovelace',
        )
        self.assertEqual(result, 'SUPER_OK')
        self.assertEqual(
            fake_req.session.get('buckaroo_googlepay_token'), 'gp-tok-abc',
        )
        self.assertEqual(
            fake_req.session.get('buckaroo_googlepay_customer_name'),
            'Ada Lovelace',
        )
        _, kwargs_to_super = super_stub.call_args
        self.assertNotIn('buckaroo_googlepay_token', kwargs_to_super)
        self.assertNotIn('buckaroo_googlepay_customer_name', kwargs_to_super)

    def test_no_googlepay_kwargs_passes_through_unchanged(self):
        result, super_stub, fake_req = self._call_controller()
        self.assertEqual(result, 'SUPER_OK')
        self.assertNotIn('buckaroo_googlepay_token', fake_req.session)
        self.assertNotIn('buckaroo_googlepay_customer_name', fake_req.session)

    def test_non_googlepay_method_does_not_stash_session(self):
        result, super_stub, fake_req = self._call_controller(
            payment_method_id=self.ideal.id,
            buckaroo_googlepay_token='leak-attempt',
            buckaroo_googlepay_customer_name='Leak',
        )
        self.assertEqual(result, 'SUPER_OK')
        self.assertNotIn('buckaroo_googlepay_token', fake_req.session)
        self.assertNotIn('buckaroo_googlepay_customer_name', fake_req.session)


@tagged('post_install', '-at_install')
class TestGooglepayCreatePayment(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.googlepay = cls.env.ref(
            'payment_buckaroo_official.payment_method_googlepay'
        )
        cls.buckaroo.payment_method_ids = [Command.link(cls.googlepay.id)]

    def test_create_payment_sends_payment_data_and_customer_name_when_session_has_them(self):
        tx = self._create_buckaroo_tx(
            reference='TX-GP-001', payment_method=self.googlepay,
        )
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_googlepay.PaymentService',
        ) as MockPS, _patch_request({
            'buckaroo_googlepay_token': 'gp-token-xyz',
            'buckaroo_googlepay_customer_name': 'Grace Hopper',
        }):
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.googlepay._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        self.assertEqual(result, mock_response)

        param_calls = {
            call.args[0]: call.args[1]
            for call in mock_builder.add_parameter.call_args_list
        }
        expected_payment_data = base64.b64encode(b'gp-token-xyz').decode('ascii')
        self.assertEqual(param_calls.get('PaymentData'), expected_payment_data)
        self.assertEqual(param_calls.get('CustomerCardName'), 'Grace Hopper')

        service_arg = MockPS.return_value.create_payment.call_args[0][0]
        self.assertEqual(service_arg, 'googlepay')

    def test_create_payment_pops_session_keys_after_use(self):
        tx = self._create_buckaroo_tx(
            reference='TX-GP-POP', payment_method=self.googlepay,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        session_dict = {
            'buckaroo_googlepay_token': 'gp-pop-token',
            'buckaroo_googlepay_customer_name': 'Linus Torvalds',
        }
        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_googlepay.PaymentService',
        ) as MockPS, _patch_request(session_dict):
            MockPS.return_value.create_payment.return_value = mock_builder
            self.googlepay._buckaroo_create_payment(tx, client)

        self.assertNotIn('buckaroo_googlepay_token', session_dict)
        self.assertNotIn('buckaroo_googlepay_customer_name', session_dict)

    def test_create_payment_raises_validation_error_when_token_missing(self):
        tx = self._create_buckaroo_tx(
            reference='TX-GP-NO-TOK', payment_method=self.googlepay,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_googlepay.PaymentService',
        ) as MockPS, _patch_request({
            'buckaroo_googlepay_customer_name': 'Token Missing',
        }):
            MockPS.return_value.create_payment.return_value = mock_builder
            with self.assertRaises(ValidationError):
                self.googlepay._buckaroo_create_payment(tx, client)

    def test_create_payment_raises_validation_error_when_customer_name_missing(self):
        tx = self._create_buckaroo_tx(
            reference='TX-GP-NO-NAME', payment_method=self.googlepay,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_googlepay.PaymentService',
        ) as MockPS, _patch_request({
            'buckaroo_googlepay_token': 'gp-token-without-name',
        }):
            MockPS.return_value.create_payment.return_value = mock_builder
            with self.assertRaises(ValidationError):
                self.googlepay._buckaroo_create_payment(tx, client)

    def test_non_googlepay_method_delegates_to_base(self):
        tx = self._create_buckaroo_tx(reference='TX-IDEAL-GP-OFF')
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService',
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        param_keys = [
            call.args[0] for call in mock_builder.add_parameter.call_args_list
        ]
        self.assertNotIn('PaymentData', param_keys)
        self.assertNotIn('CustomerCardName', param_keys)


@tagged('post_install', '-at_install')
class TestGooglepayRefundDelegatesToBase(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.googlepay = cls.env.ref(
            'payment_buckaroo_official.payment_method_googlepay'
        )
        cls.buckaroo.payment_method_ids = [Command.link(cls.googlepay.id)]

    def test_refund_delegates_to_base_without_googlepay_params(self):
        source_tx = self._create_buckaroo_tx(
            reference='SRC-GP-REF', amount=50.0, payment_method=self.googlepay,
        )
        source_tx.provider_reference = 'SRC_GP_KEY'
        refund_tx = self._create_buckaroo_tx(
            reference='REF-GP-001', amount=-50.0, payment_method=self.googlepay,
        )
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService',
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.googlepay._buckaroo_create_refund(
                source_tx, refund_tx, client,
            )

        mock_builder.refund.assert_called_once()
        self.assertEqual(result, mock_response)

        params = MockPS.return_value.create_payment.call_args[0][1]
        self.assertNotIn('paymentData', params)
        self.assertNotIn('PaymentData', params)
        self.assertNotIn('customerCardName', params)
        self.assertNotIn('CustomerCardName', params)

        param_keys = [
            call.args[0] for call in mock_builder.add_parameter.call_args_list
        ]
        self.assertNotIn('PaymentData', param_keys)
        self.assertNotIn('CustomerCardName', param_keys)

        service_arg = MockPS.return_value.create_payment.call_args[0][0]
        self.assertEqual(service_arg, 'googlepay')


@tagged('post_install', '-at_install')
class TestGooglepayCompatibilityFilter(BuckarooOfficialCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.googlepay = cls.env.ref(
            'payment_buckaroo_official.payment_method_googlepay'
        )

    def _compatible_codes(self):
        methods = self.env['payment.method']._get_compatible_payment_methods(
            provider_ids=self.buckaroo.ids,
            partner_id=self.env.ref('base.partner_admin').id,
            currency_id=self.env.ref('base.EUR').id,
        )
        return set(methods.mapped('code'))

    def test_googlepay_hidden_when_merchant_guid_unconfigured(self):
        self.googlepay.buckaroo_official_googlepay_merchant_guid = ''
        self.assertNotIn('googlepay', self._compatible_codes())

    def test_googlepay_visible_when_merchant_guid_set(self):
        self.googlepay.buckaroo_official_googlepay_merchant_guid = 'BUCK-GUID-AAA'
        self.assertIn('googlepay', self._compatible_codes())

    def test_googlepay_hidden_in_prod_when_google_merchant_id_unset(self):
        self.buckaroo.state = 'enabled'
        self.googlepay.write({
            'buckaroo_official_googlepay_merchant_guid': 'BUCK-GUID-AAA',
            'buckaroo_official_googlepay_google_merchant_id': '',
        })
        self.assertNotIn('googlepay', self._compatible_codes())

    def test_googlepay_visible_in_prod_when_fully_configured(self):
        self.buckaroo.state = 'enabled'
        self.googlepay.write({
            'buckaroo_official_googlepay_merchant_guid': 'BUCK-GUID-AAA',
            'buckaroo_official_googlepay_google_merchant_id': 'GOOG-MID-BBB',
        })
        self.assertIn('googlepay', self._compatible_codes())

    def test_googlepay_visible_in_test_without_google_merchant_id(self):
        self.buckaroo.state = 'test'
        self.googlepay.write({
            'buckaroo_official_googlepay_merchant_guid': 'BUCK-GUID-AAA',
            'buckaroo_official_googlepay_google_merchant_id': '',
        })
        self.assertIn('googlepay', self._compatible_codes())
