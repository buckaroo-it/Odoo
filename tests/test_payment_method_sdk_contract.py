# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""SDK-call contract per payment method.

Asserts that every payment method with per-method overrides honors its
contract with the Buckaroo SDK: the right service name and the right
method-specific params for each operation it supports.
"""

from unittest.mock import MagicMock, patch

from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon, make_mock_sdk_builder


def _patch_request(session_dict):
    """Patch odoo.http.request with a mock carrying session_dict."""
    mock_req = MagicMock()
    mock_req.session = session_dict
    return patch('odoo.http.request', mock_req)


def _invoke_verb(pm_record, verb, tx, client, *, source_tx=None, session=None):
    """Run the public code path on *pm_record* that drives *verb*.

    Kept in one place so verbs and code paths map 1:1. The tests below
    read more clearly by naming the (method, verb) cell and letting this
    helper pick the right dispatch.
    """
    if verb == 'refund':
        return pm_record._buckaroo_create_refund(source_tx, tx, client)
    if verb == 'capture':
        return pm_record._buckaroo_create_capture(tx, client)
    if verb == 'cancelAuthorize':
        return pm_record._buckaroo_create_void(tx, client)
    # pay / authorize / *WithToken are all driven by _buckaroo_create_payment;
    # which SDK verb ultimately fires depends on the record's state (authorize
    # config + HF session dict). Callers set those before calling us.
    with _patch_request(session or {}):
        return pm_record._buckaroo_create_payment(tx, client)


def _patched_payment_service(module_path):
    """Patch the PaymentService symbol in the given module path."""
    return patch(f'{module_path}.PaymentService')


@tagged('post_install', '-at_install')
class TestCreditcardSdkContract(BuckarooOfficialCommon):
    """Exercise every SDK verb creditcard supports and assert on injected params."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref(
            'payment_buckaroo_official.payment_method_creditcard'
        )
        cls.buckaroo.payment_method_ids = [Command.link(cls.creditcard.id)]

    def _make_source_authorize_tx(self, reference, service_code='visa'):
        """Build a source transaction with a recorded service code for brand lookup."""
        tx = self._create_buckaroo_tx(
            reference=reference, amount=50.0, payment_method=self.creditcard,
        )
        tx.provider_reference = f'{reference}_KEY'
        tx.buckaroo_official_service_code = service_code
        return tx

    def _assert_service_name(self, mock_ps, expected='creditcard'):
        call = mock_ps.return_value.create_payment.call_args
        self.assertEqual(call[0][0], expected)

    def _assert_params_include_brand(self, mock_ps, brand):
        params = mock_ps.return_value.create_payment.call_args[0][1]
        self.assertEqual(params.get('brand'), brand)


    def test_creditcard_refund_sends_brand_to_sdk(self):
        """Creditcard refund passes brand from source tx service_code."""
        source_tx = self._make_source_authorize_tx(
            'SRC-CC-MX-REF', service_code='visa',
        )
        refund_tx = self._create_buckaroo_tx(
            reference='REF-CC-MX-REF', amount=-50.0, payment_method=self.creditcard,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with _patched_payment_service(
            'odoo.addons.payment_buckaroo_official.models.payment_method'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            _invoke_verb(self.creditcard, 'refund', refund_tx, client,
                         source_tx=source_tx)

        self._assert_service_name(MockPS)
        self._assert_params_include_brand(MockPS, 'visa')
        mock_builder.refund.assert_called_once()


    def test_creditcard_capture_sends_brand_to_sdk(self):
        """Creditcard capture passes brand from source tx service_code."""
        source_tx = self._make_source_authorize_tx(
            'SRC-CC-MX-CAP', service_code='mastercard',
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with _patched_payment_service(
            'odoo.addons.payment_buckaroo_official.models.payment_method'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            _invoke_verb(self.creditcard, 'capture', source_tx, client)

        self._assert_service_name(MockPS)
        self._assert_params_include_brand(MockPS, 'mastercard')
        mock_builder.capture.assert_called_once()


    def test_creditcard_cancel_authorize_sends_brand_to_sdk(self):
        """Creditcard void passes brand from source tx service_code."""
        source_tx = self._make_source_authorize_tx(
            'SRC-CC-MX-VOID', service_code='amex',
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with _patched_payment_service(
            'odoo.addons.payment_buckaroo_official.models.payment_method'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            _invoke_verb(self.creditcard, 'cancelAuthorize', source_tx, client)

        self._assert_service_name(MockPS)
        self._assert_params_include_brand(MockPS, 'amex')
        mock_builder.cancelAuthorize.assert_called_once()


    def test_creditcard_pay_redirect_uses_creditcard_service(self):
        """Redirect + pay: SDK called with 'creditcard' and .pay() invoked."""
        self.creditcard.buckaroo_official_creditcard_authorize = 'pay'
        self.creditcard.buckaroo_official_creditcard_method = 'redirect'
        tx = self._create_buckaroo_tx(
            reference='TX-CC-MX-PAY', payment_method=self.creditcard,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with _patched_payment_service(
            'odoo.addons.payment_buckaroo_official.models.payment_method_creditcard'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            _invoke_verb(self.creditcard, 'pay', tx, client)

        self._assert_service_name(MockPS)
        mock_builder.pay.assert_called_once()
        mock_builder.authorize.assert_not_called()


    def test_creditcard_authorize_redirect_uses_creditcard_service(self):
        """Redirect + authorize: SDK called with 'creditcard' and .authorize()."""
        self.creditcard.buckaroo_official_creditcard_authorize = 'authorize'
        self.creditcard.buckaroo_official_creditcard_method = 'redirect'
        tx = self._create_buckaroo_tx(
            reference='TX-CC-MX-AUTH', payment_method=self.creditcard,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with _patched_payment_service(
            'odoo.addons.payment_buckaroo_official.models.payment_method_creditcard'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            _invoke_verb(self.creditcard, 'authorize', tx, client)

        self._assert_service_name(MockPS)
        mock_builder.authorize.assert_called_once()
        mock_builder.pay.assert_not_called()


    def test_creditcard_pay_with_token_injects_session_and_brand(self):
        """Inline HF + pay: SessionId added, brand set from service, .payWithToken()."""
        self.creditcard.buckaroo_official_creditcard_authorize = 'pay'
        tx = self._create_buckaroo_tx(
            reference='TX-CC-MX-PWT', payment_method=self.creditcard,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with _patched_payment_service(
            'odoo.addons.payment_buckaroo_official.models.payment_method_creditcard'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            _invoke_verb(
                self.creditcard, 'payWithToken', tx, client,
                session={
                    'buckaroo_hf_session_id': 'hf-mx-pwt',
                    'buckaroo_hf_service': 'visa',
                },
            )

        self._assert_service_name(MockPS)
        self._assert_params_include_brand(MockPS, 'visa')
        mock_builder.add_parameter.assert_any_call('SessionId', 'hf-mx-pwt')
        mock_builder.payWithToken.assert_called_once()


    def test_creditcard_authorize_with_token_injects_session_and_brand(self):
        """Inline HF + authorize: brand set, SessionId added, .authorizeWithToken()."""
        self.creditcard.buckaroo_official_creditcard_authorize = 'authorize'
        tx = self._create_buckaroo_tx(
            reference='TX-CC-MX-AWT', payment_method=self.creditcard,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with _patched_payment_service(
            'odoo.addons.payment_buckaroo_official.models.payment_method_creditcard'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            _invoke_verb(
                self.creditcard, 'authorizeWithToken', tx, client,
                session={
                    'buckaroo_hf_session_id': 'hf-mx-awt',
                    'buckaroo_hf_service': 'mastercard',
                },
            )

        self._assert_service_name(MockPS)
        self._assert_params_include_brand(MockPS, 'mastercard')
        mock_builder.add_parameter.assert_any_call('SessionId', 'hf-mx-awt')
        mock_builder.authorizeWithToken.assert_called_once()


@tagged('post_install', '-at_install')
class TestBillinkSdkContract(BuckarooOfficialCommon):
    """Exercise every SDK verb billink supports and assert on injected params."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.billink = cls.env.ref('payment_buckaroo_official.payment_method_billink')
        cls.buckaroo.payment_method_ids = [Command.link(cls.billink.id)]
        cls.partner_nl = cls.env['res.partner'].create({
            'name': 'Jan de Vries',
            'street': 'Keizersgracht 424',
            'zip': '1016 GC',
            'city': 'Amsterdam',
            'country_id': cls.env.ref('base.nl').id,
            'email': 'jan@example.nl',
            'phone': '+31612345678',
        })

    def _create_billink_tx(self, reference, amount=50.0):
        return self.env['payment.transaction'].create({
            'provider_id': self.buckaroo.id,
            'payment_method_id': self.billink.id,
            'reference': reference,
            'amount': amount,
            'currency_id': self.currency_euro.id,
            'partner_id': self.partner_nl.id,
            'operation': 'online_redirect',
        })


    def test_billink_pay_injects_article_and_customer_params(self):
        """Billink pay adds article, billingCustomer, shippingCustomer via add_parameter."""
        tx = self._create_billink_tx('TX-BL-MX-PAY')
        product = self.env['product.product'].create({
            'name': 'Test Widget',
            'default_code': 'WIDGET-MX',
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
        mock_builder, _ = make_mock_sdk_builder()

        with _patched_payment_service(
            'odoo.addons.payment_buckaroo_official.models.payment_method_billink'
        ) as MockPS, patch(
            'odoo.addons.payment_buckaroo_official.models.payment_method_billink'
            '.PaymentMethodBillink._get_birthdate_from_session',
            return_value='',
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            _invoke_verb(self.billink, 'pay', tx, client)

        call = MockPS.return_value.create_payment.call_args
        self.assertEqual(call[0][0], 'billink')
        param_names = [c[0][0] for c in mock_builder.add_parameter.call_args_list]
        self.assertIn('article', param_names)
        self.assertIn('billingCustomer', param_names)
        self.assertIn('shippingCustomer', param_names)
        mock_builder.pay.assert_called_once()


    def test_billink_refund_delegates_without_method_specific_params(self):
        """Billink refund delegates to super — no brand, no article list leakage."""
        source_tx = self._create_billink_tx('SRC-BL-MX-REF')
        source_tx.provider_reference = 'SRC-BL-MX-REF_KEY'
        refund_tx = self._create_billink_tx('REF-BL-MX-REF', amount=-50.0)
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with _patched_payment_service(
            'odoo.addons.payment_buckaroo_official.models.payment_method'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            _invoke_verb(self.billink, 'refund', refund_tx, client,
                         source_tx=source_tx)

        params = MockPS.return_value.create_payment.call_args[0][1]
        self.assertNotIn('brand', params)
        # Billink's _buckaroo_create_payment is not on the refund path, so
        # article/customer params must not leak into refund.
        mock_builder.add_parameter.assert_not_called()
        mock_builder.refund.assert_called_once()


    def test_billink_capture_delegates_without_method_specific_params(self):
        """Billink capture delegates to super — no brand, no article list."""
        tx = self._create_billink_tx('TX-BL-MX-CAP')
        tx.provider_reference = 'TX-BL-MX-CAP_KEY'
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with _patched_payment_service(
            'odoo.addons.payment_buckaroo_official.models.payment_method'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            _invoke_verb(self.billink, 'capture', tx, client)

        params = MockPS.return_value.create_payment.call_args[0][1]
        self.assertNotIn('brand', params)
        mock_builder.add_parameter.assert_not_called()
        mock_builder.capture.assert_called_once()


    def test_billink_cancel_authorize_delegates_without_method_specific_params(self):
        """Billink void delegates to super — no brand, no article list."""
        tx = self._create_billink_tx('TX-BL-MX-VOID')
        tx.provider_reference = 'TX-BL-MX-VOID_KEY'
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with _patched_payment_service(
            'odoo.addons.payment_buckaroo_official.models.payment_method'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            _invoke_verb(self.billink, 'cancelAuthorize', tx, client)

        params = MockPS.return_value.create_payment.call_args[0][1]
        self.assertNotIn('brand', params)
        mock_builder.add_parameter.assert_not_called()
        mock_builder.cancelAuthorize.assert_called_once()


@tagged('post_install', '-at_install')
class TestBaseMethodsSdkContract(BuckarooOfficialCommon):
    """Methods without per-method overrides must cleanly delegate to base SDK calls."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bancontact = cls.env.ref(
            'payment_buckaroo_official.payment_method_bancontact'
        )
        cls.buckaroo.payment_method_ids = [Command.link(cls.bancontact.id)]

    def _run_verb_and_get_params(self, pm, verb):
        tx = self._create_buckaroo_tx(
            reference=f'TX-BASE-{pm.code.upper()}-{verb.upper()}',
            payment_method=pm,
        )
        tx.provider_reference = f'{pm.code}_KEY_{verb}'

        source_tx = None
        if verb == 'refund':
            source_tx = tx
            tx = self._create_buckaroo_tx(
                reference=f'REF-BASE-{pm.code.upper()}',
                amount=-50.0,
                payment_method=pm,
            )

        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with _patched_payment_service(
            'odoo.addons.payment_buckaroo_official.models.payment_method'
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            _invoke_verb(pm, verb, tx, client, source_tx=source_tx)

        return MockPS.return_value.create_payment.call_args, mock_builder

    def test_ideal_refund_has_no_brand_and_calls_refund(self):
        """iDEAL refund does not inject brand or article params."""
        call, builder = self._run_verb_and_get_params(self.ideal, 'refund')
        params = call[0][1]
        self.assertNotIn('brand', params)
        self.assertEqual(call[0][0], 'ideal')
        builder.refund.assert_called_once()
        builder.add_parameter.assert_not_called()

    def test_bancontact_refund_has_no_brand_and_calls_refund(self):
        """Bancontact refund delegates cleanly to base — no brand, no article."""
        call, builder = self._run_verb_and_get_params(self.bancontact, 'refund')
        params = call[0][1]
        self.assertNotIn('brand', params)
        self.assertEqual(call[0][0], 'bancontact')
        builder.refund.assert_called_once()
        builder.add_parameter.assert_not_called()

    def test_ideal_capture_has_no_brand_and_calls_capture(self):
        """iDEAL capture delegates cleanly."""
        call, builder = self._run_verb_and_get_params(self.ideal, 'capture')
        params = call[0][1]
        self.assertNotIn('brand', params)
        self.assertEqual(call[0][0], 'ideal')
        builder.capture.assert_called_once()
        builder.add_parameter.assert_not_called()

    def test_bancontact_cancel_authorize_has_no_brand(self):
        """Bancontact void delegates cleanly."""
        call, builder = self._run_verb_and_get_params(
            self.bancontact, 'cancelAuthorize',
        )
        params = call[0][1]
        self.assertNotIn('brand', params)
        self.assertEqual(call[0][0], 'bancontact')
        builder.cancelAuthorize.assert_called_once()
        builder.add_parameter.assert_not_called()
