# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""End-to-end + unit tests for the Riverty payment method.

Covers payment creation and the pay/authorize switch, webhook state
transitions, refund lifecycle, capture/void dispatch (authorize flow),
amount/country gating, shop-controller validation (birthdate +
underage), riverty-only dispatch on payment.method, the dict→Riverty
API formatters (article + customer + ImageUrl resolver), and the
birthdate-from-session flow. SDK transport is mocked throughout.
"""

from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import BaseCase, tagged

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
from ..helpers.customer import get_customer_data
from ..models.payment_method_riverty import PaymentMethodRiverty as RivertyPaymentMethod
from .test_helpers import make_partner


@tagged("post_install", "-at_install")
class TestRivertyPaymentCreation(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.riverty = cls.env.ref("payment_buckaroo_official.payment_method_riverty")
        cls.buckaroo.payment_method_ids = [Command.link(cls.riverty.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {
                "name": "Jan de Vries",
                "country_id": cls.env.ref("base.nl").id,
                "email": "jan@example.nl",
            }
        )

    def _sdk_response(self, redirect_url="https://testcheckout.buckaroo.nl/pay/RV", key="RV_KEY"):
        response = MagicMock()
        response.get_redirect_url.return_value = redirect_url
        response.key = key
        response.status_code = 200
        response.redirect_url = redirect_url
        response.required_action = None
        response.buckaroo_status_message = "Success"
        response._raw_data = {}
        return response

    def _create_tx(self, reference="RIVERTY-TX-001", amount=100.0):
        return self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.riverty.id,
                "reference": reference,
                "amount": amount,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )

    def test_payment_creation_returns_redirect_and_stores_reference(self):
        response = self._sdk_response(redirect_url="https://rv/pay/X", key="STORED_KEY")
        tx = self._create_tx()
        PaymentMethod = type(tx.payment_method_id)

        with (
            patch.object(PaymentMethod, "_buckaroo_create_payment", return_value=response),
            patch.object(
                PaymentMethod, "_buckaroo_extract_redirect_url", return_value="https://rv/pay/X"
            ),
        ):
            result = tx._get_specific_processing_values({})

        self.assertEqual(result["api_url"], "https://rv/pay/X")
        self.assertEqual(tx.provider_reference, "STORED_KEY")

    def test_payment_creation_missing_redirect_and_failure_raises(self):
        # No redirect URL AND a failure status: the no-redirect handler can't
        # settle it (returns None), so the flow falls through to the error
        # path. A no-redirect SUCCESS instead settles synchronously — covered
        # by TestRivertyNoRedirectSettlement.
        response = make_mock_sdk_response(490)
        tx = self._create_tx()
        PaymentMethod = type(tx.payment_method_id)

        with (
            patch.object(PaymentMethod, "_buckaroo_create_payment", return_value=response),
            patch.object(PaymentMethod, "_buckaroo_extract_redirect_url", return_value=None),
        ):
            with self.assertRaises(ValidationError):
                tx._get_specific_processing_values({})


@tagged("post_install", "-at_install")
class TestRivertyWebhookCallbacks(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.riverty = cls.env.ref("payment_buckaroo_official.payment_method_riverty")
        cls.buckaroo.payment_method_ids = [Command.link(cls.riverty.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {
                "name": "Jan de Vries",
                "country_id": cls.env.ref("base.nl").id,
            }
        )

    def _create_tx(self, reference, amount=100.0):
        return self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.riverty.id,
                "reference": reference,
                "amount": amount,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )

    def _form_callback(self, reference, status_code, txn_key="RIVERTY_TXN_001"):
        return parsed_from_form(
            {
                "brq_invoicenumber": reference,
                "brq_amount": "100.00",
                "brq_currency": "EUR",
                "brq_statuscode": str(status_code),
                "brq_transactions": txn_key,
            }
        )

    def _json_callback(self, reference, status_code, txn_key="RIVERTY_TXN_001"):
        return parsed_from_json(
            {
                "Transaction": {
                    "Invoice": reference,
                    "AmountDebit": 100.0,
                    "Currency": "EUR",
                    "Status": {"Code": {"Code": status_code, "Description": "Test"}},
                    "Key": txn_key,
                },
            }
        )

    def _assert_status_maps_to_state(self, codes, expected_state):
        for code in codes:
            for fmt in ("form", "json"):
                with self.subTest(code=code, handler=fmt):
                    ref = "RIVERTY-WH-%s-%s" % (fmt.upper(), code)
                    tx = self._create_tx(ref)
                    builder = self._form_callback if fmt == "form" else self._json_callback
                    parsed = builder(ref, code)
                    self.env["payment.transaction"].sudo()._process("buckaroo_official", parsed)
                    self.assertEqual(tx.state, expected_state)

    def test_done_status_maps_to_done(self):
        self._assert_status_maps_to_state(CALLBACK_DONE_CODES, "done")
        tx = self._create_tx("RIVERTY-WH-REF-190")
        parsed = self._form_callback("RIVERTY-WH-REF-190", 190)
        self.env["payment.transaction"].sudo()._process("buckaroo_official", parsed)
        self.assertEqual(tx.provider_reference, "RIVERTY_TXN_001")

    def test_pending_status_maps_to_pending(self):
        self._assert_status_maps_to_state(CALLBACK_PENDING_CODES, "pending")

    def test_cancel_status_maps_to_cancel(self):
        self._assert_status_maps_to_state(CALLBACK_CANCEL_CODES, "cancel")

    def test_error_status_maps_to_error(self):
        self._assert_status_maps_to_state(CALLBACK_ERROR_CODES, "error")


@tagged("post_install", "-at_install")
class TestRivertyRefundFlow(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.riverty = cls.env.ref("payment_buckaroo_official.payment_method_riverty")
        cls.buckaroo.payment_method_ids = [Command.link(cls.riverty.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {
                "name": "Jan de Vries",
                "country_id": cls.env.ref("base.nl").id,
            }
        )

    def _create_done_tx(self, reference="RV-DONE", amount=100.0):
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.riverty.id,
                "reference": reference,
                "amount": amount,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )
        tx._apply_updates(
            parsed_from_form(
                {
                    "brq_invoicenumber": reference,
                    "brq_amount": str(amount),
                    "brq_currency": "EUR",
                    "brq_statuscode": "190",
                    "brq_transactions": "RIVERTY_DONE_KEY",
                }
            )
        )
        self.assertEqual(tx.state, "done")
        return tx

    def _mock_refund_response(self, key="RV_REFUND_KEY", status_code=190):
        response = make_mock_sdk_response(status_code)
        response.key = key
        return response

    def _patch_refund(self, response):
        PaymentMethod = type(self.env["payment.method"])
        return patch.object(PaymentMethod, "_buckaroo_create_refund", return_value=response)

    def test_refund_creates_linked_child_tx(self):
        tx = self._create_done_tx(reference="RV-REFUND-CREATE")
        with self._patch_refund(self._mock_refund_response()):
            refund_tx = tx._refund()

        self.assertEqual(refund_tx.operation, "refund")
        self.assertEqual(refund_tx.source_transaction_id, tx)
        self.assertIn("R-", refund_tx.reference)

    def test_refund_status_matrix(self):
        for status_code, expected_state in REFUND_STATUS_CASES:
            with self.subTest(status_code=status_code, expected_state=expected_state):
                tx = self._create_done_tx(reference="RV-REFUND-%s" % status_code)
                with self._patch_refund(
                    self._mock_refund_response(
                        key="RV_REFUND_%s" % status_code,
                        status_code=status_code,
                    )
                ):
                    refund_tx = tx._refund()
                self.assertEqual(refund_tx.state, expected_state)

    def test_partial_refund_marks_refund_tx_as_error(self):
        """Riverty partial refund needs an article-level breakdown that
        Odoo's amount-based refund flow cannot supply. Refuse loudly so
        Odoo flags the refund_tx as error rather than send an
        unbalanced request and let Riverty 491.

        Odoo's ``_refund`` wraps ``_send_refund_request`` in a
        ``try/except ValidationError`` that calls ``_set_error`` on the
        refund tx — so the test asserts on the resulting state, not on
        a propagated exception.
        """
        tx = self._create_done_tx(reference="RV-PARTIAL", amount=100.0)
        refund_tx = tx._refund(amount_to_refund=25.0)
        self.assertEqual(refund_tx.state, "error")
        self.assertIn("partial refunds", refund_tx.state_message.lower())


@tagged("post_install", "-at_install")
class TestRivertyAmountLimitEnforcement(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.riverty = cls.env.ref("payment_buckaroo_official.payment_method_riverty")
        cls.buckaroo.payment_method_ids = [Command.link(cls.riverty.id)]
        cls.riverty.buckaroo_official_max_amount = "1500.00"
        cls.partner_nl = cls.env["res.partner"].create(
            {
                "name": "NL Partner",
                "country_id": cls.env.ref("base.nl").id,
            }
        )

    def _riverty_visible(self, amount):
        methods = self.env["payment.method"]._get_compatible_payment_methods(
            self.buckaroo.ids,
            self.partner_nl.id,
            currency_id=self.currency_euro.id,
            amount=amount,
        )
        return self.riverty in methods

    def test_below_limit_visible(self):
        self.assertTrue(self._riverty_visible(500.0))

    def test_at_limit_visible(self):
        self.assertTrue(self._riverty_visible(1500.0))

    def test_above_limit_hidden(self):
        self.assertFalse(self._riverty_visible(1800.0))


@tagged("post_install", "-at_install")
class TestRivertyCurrencyAndCountryRestriction(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.riverty = cls.env.ref("payment_buckaroo_official.payment_method_riverty")
        cls.buckaroo.payment_method_ids = [Command.link(cls.riverty.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {
                "name": "NL",
                "country_id": cls.env.ref("base.nl").id,
            }
        )
        cls.partner_us = cls.env["res.partner"].create(
            {
                "name": "US",
                "country_id": cls.env.ref("base.us").id,
            }
        )

    def _visible(self, partner, currency=None):
        methods = self.env["payment.method"]._get_compatible_payment_methods(
            self.buckaroo.ids,
            partner.id,
            currency_id=(currency or self.currency_euro).id,
            amount=100.0,
        )
        return self.riverty in methods

    def test_eur_nl_visible(self):
        self.assertTrue(self._visible(self.partner_nl))

    def test_usd_currency_hidden(self):
        self.assertFalse(self._visible(self.partner_nl, currency=self.env.ref("base.USD")))

    def test_us_country_hidden(self):
        self.assertFalse(self._visible(self.partner_us))


@tagged("post_install", "-at_install")
class TestRivertyShopPaymentControllerValidations(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.riverty = cls.env.ref("payment_buckaroo_official.payment_method_riverty")
        cls.buckaroo.payment_method_ids = [Command.link(cls.riverty.id)]

    def _invoke(self, **kwargs):
        """Fire the Riverty controller handler; it raises before super() is reached."""
        import odoo.http
        from odoo.addons.payment_buckaroo_official.controllers.riverty import (
            RivertyPaymentPortal,
        )

        controller = RivertyPaymentPortal()

        mock_request = MagicMock()
        mock_request.env = self.env
        mock_request.session = {}
        with (
            patch.object(odoo.http, "request", mock_request),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.riverty.request",
                new=mock_request,
            ),
            patch(
                "odoo.addons.portal.controllers.portal.request",
                new=mock_request,
            ),
        ):
            return controller.shop_payment_transaction(
                order_id=1,
                access_token="ignored",
                payment_method_id=self.riverty.id,
                **kwargs,
            )

    def test_missing_birthdate_raises(self):
        with self.assertRaises(ValidationError) as ctx:
            self._invoke()
        self.assertIn("date of birth", str(ctx.exception))

    def test_invalid_birthdate_format_raises(self):
        with self.assertRaises(ValidationError) as ctx:
            self._invoke(riverty_birthdate="not-a-date")
        self.assertIn("Invalid date of birth", str(ctx.exception))

    def test_under_18_raises(self):
        with self.assertRaises(ValidationError) as ctx:
            self._invoke(riverty_birthdate="2020-01-01")
        self.assertIn("18", str(ctx.exception))

    def test_one_day_before_18th_birthday_raises(self):
        """Boundary: 17y 364d must fail. ``(today - dob).days // 365``
        rounds up to 18 across leap-year crossings; the year-difference
        calc with a (month, day) tiebreak does not."""
        from datetime import date as _date
        from datetime import timedelta as _td

        tomorrow_18th_bday = _date.today() + _td(days=1)
        eighteen_years_minus_a_day = _date(
            tomorrow_18th_bday.year - 18,
            tomorrow_18th_bday.month,
            tomorrow_18th_bday.day,
        ).isoformat()
        with self.assertRaises(ValidationError) as ctx:
            self._invoke(riverty_birthdate=eighteen_years_minus_a_day)
        self.assertIn("18", str(ctx.exception))

    def test_valid_birthdate_persists_on_logged_in_user_partner(self):
        from datetime import date

        partner = self.env.user.partner_id
        partner.buckaroo_riverty_birthdate = False
        with patch(
            "odoo.addons.website_sale.controllers.payment.PaymentPortal.shop_payment_transaction",
            return_value="SUPER_OK",
        ):
            # order_id=1 (the ``_invoke`` fixture default) is a B2B
            # order in this dev database; supply an identification
            # number so the new B2B guard doesn't block this
            # birthdate-persistence assertion.
            self._invoke(
                riverty_birthdate="1990-05-15",
                riverty_identification_number="12345678",
            )
        self.assertEqual(partner.buckaroo_riverty_birthdate, date(1990, 5, 15))

    def test_b2b_order_missing_identification_number_raises(self):
        """A B2B order (company invoice partner) must supply an
        identification number to proceed with Riverty, enforced via the
        shared ``validate_bnpl_registry`` helper."""
        import odoo.http
        from odoo.addons.payment_buckaroo_official.controllers.riverty import (
            RivertyPaymentPortal,
        )

        company_partner = self.env["res.partner"].create(
            {"name": "Acme BV", "is_company": True}
        )
        order = self.env["sale.order"].create({"partner_id": company_partner.id})
        controller = RivertyPaymentPortal()
        mock_request = MagicMock()
        mock_request.env = self.env
        mock_request.session = {}
        with (
            patch.object(odoo.http, "request", mock_request),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.riverty.request",
                new=mock_request,
            ),
            patch(
                "odoo.addons.portal.controllers.portal.request",
                new=mock_request,
            ),
        ):
            with self.assertRaises(ValidationError) as ctx:
                controller.shop_payment_transaction(
                    order_id=order.id,
                    access_token="ignored",
                    payment_method_id=self.riverty.id,
                    riverty_birthdate="1990-05-15",
                    riverty_salutation="Mr",
                )
        self.assertIn("identification number", str(ctx.exception).lower())

    def test_b2c_order_identification_number_kwarg_popped_before_super(self):
        """A B2C order doesn't require an identification number; a
        stray value must still be popped so it never leaks into the
        super() call's kwargs."""
        import odoo.http
        from odoo.addons.payment_buckaroo_official.controllers.riverty import (
            RivertyPaymentPortal,
        )

        person_partner = self.env["res.partner"].create({"name": "Jan de Vries"})
        order = self.env["sale.order"].create({"partner_id": person_partner.id})
        controller = RivertyPaymentPortal()
        mock_request = MagicMock()
        mock_request.env = self.env
        mock_request.session = {}
        captured = {}

        def fake_super(order_id, access_token, **kwargs):
            captured.update(kwargs)
            return "SUPER_OK"

        with (
            patch.object(odoo.http, "request", mock_request),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.riverty.request",
                new=mock_request,
            ),
            patch(
                "odoo.addons.portal.controllers.portal.request",
                new=mock_request,
            ),
            patch(
                "odoo.addons.website_sale.controllers.payment.PaymentPortal."
                "shop_payment_transaction",
                side_effect=fake_super,
            ),
        ):
            result = controller.shop_payment_transaction(
                order_id=order.id,
                access_token="ignored",
                payment_method_id=self.riverty.id,
                riverty_birthdate="1990-05-15",
                riverty_salutation="Mr",
                riverty_identification_number="12345678",
            )
        self.assertEqual(result, "SUPER_OK")
        self.assertNotIn("riverty_identification_number", captured)

    def test_non_riverty_method_passes_through(self):
        """Non-riverty payment methods must NOT trigger birthdate validation."""
        import odoo.http
        from odoo.addons.payment_buckaroo_official.controllers.riverty import (
            RivertyPaymentPortal,
        )

        controller = RivertyPaymentPortal()
        mock_request = MagicMock()
        mock_request.env = self.env
        mock_request.session = {}
        with (
            patch.object(odoo.http, "request", mock_request),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.riverty.request",
                new=mock_request,
            ),
            patch(
                "odoo.addons.website_sale.controllers.payment.PaymentPortal.shop_payment_transaction",
                return_value="SUPER_OK",
            ),
        ):
            result = controller.shop_payment_transaction(
                order_id=1,
                access_token="ignored",
                payment_method_id=self.ideal.id,
            )
        self.assertEqual(result, "SUPER_OK")


@tagged("post_install", "-at_install")
class TestRivertyCreatePaymentDispatch(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.riverty = cls.env.ref("payment_buckaroo_official.payment_method_riverty")
        cls.buckaroo.payment_method_ids = [Command.link(cls.riverty.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {
                "name": "Jan",
                "street": "Keizersgracht 424",
                "zip": "1016 GC",
                "city": "Amsterdam",
                "country_id": cls.env.ref("base.nl").id,
                "email": "jan@example.nl",
                "phone": "+31612345678",
            }
        )

    def setUp(self):
        # Reset per-method authorize flag to 'pay' default. Prior tests
        # in this class (test_riverty_authorize_calls_sdk_authorize) and
        # sibling classes (CaptureAndVoid, AuthorizeWebhook) toggle this
        # field; the ORM cache on cls.riverty can retain the toggled
        # value across savepoint rollbacks, so write the default
        # explicitly each test.
        super().setUp()
        self.riverty.buckaroo_official_riverty_authorize = "pay"
        # Pre-set salutation on the test partner so the NL/BE
        # salutation-required guard in ``_buckaroo_create_payment``
        # passes for the dispatch tests that don't explicitly drive the
        # session value.
        self.partner_nl.buckaroo_riverty_salutation = "Mr"

    def _make_tx_with_product(self, reference="TX-RV-001", amount=50.0):
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.riverty.id,
                "reference": reference,
                "amount": amount,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )
        product = self.env["product.product"].create(
            {
                "name": "Widget",
                "default_code": "WIDGET-RV-01",
                "list_price": amount,
            }
        )
        order = self.env["sale.order"].create({"partner_id": self.partner_nl.id})
        self.env["sale.order.line"].create(
            {
                "order_id": order.id,
                "product_id": product.id,
                "product_uom_qty": 1,
                "price_unit": amount,
            }
        )
        tx.sale_order_ids = [Command.link(order.id)]
        return tx

    def test_non_riverty_skips_add_parameter(self):
        tx = self._create_buckaroo_tx(reference="TX-NB-RV", payment_method=self.ideal)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        mock_builder.add_parameter.assert_not_called()
        self.assertEqual(result, mock_response)

    @staticmethod
    def _patch_dispatch_context(birthdate="15-05-1990", salutation="Mr"):
        """Patch ``PaymentService`` + the helpers ``_buckaroo_create_payment``
        uses to source birthdate / salutation, in one place."""
        ps_patch = patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_riverty.PaymentService"
        )
        birthdate_patch = patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_riverty.resolve_birthdate",
            return_value=birthdate,
        )
        # ``pop_session_value`` is called once for the salutation key in
        # ``_buckaroo_create_payment`` (the partner-fallback chain). The
        # partner attr is set by the test setUp, so this only needs to
        # control the session leg.
        salutation_patch = patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_riverty.pop_session_value",
            return_value=salutation,
        )
        return ps_patch, birthdate_patch, salutation_patch

    def test_riverty_pay_adds_article_and_customer_parameters(self):
        tx = self._make_tx_with_product()
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        ps, dob, sal = self._patch_dispatch_context()
        with ps as MockPS, dob, sal:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.riverty._buckaroo_create_payment(tx, client)

        params_by_name = {
            call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list
        }
        self.assertIn("article", params_by_name)
        self.assertIn("billingCustomer", params_by_name)
        self.assertIn("shippingCustomer", params_by_name)

        article = params_by_name["article"][0]
        self.assertEqual(article["Identifier"], "WIDGET-RV-01")
        self.assertEqual(article["Quantity"], "1")

        billing = params_by_name["billingCustomer"][0]
        self.assertEqual(billing["BirthDate"], "15-05-1990")
        self.assertEqual(billing["Salutation"], "Mr")
        self.assertEqual(billing["Country"], "NL")

        mock_builder.pay.assert_called_once()
        mock_builder.authorize.assert_not_called()
        self.assertEqual(result, mock_response)

    def test_riverty_authorize_calls_sdk_authorize(self):
        self.riverty.buckaroo_official_riverty_authorize = "authorize"
        tx = self._make_tx_with_product(reference="TX-RV-AUTH")
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        ps, dob, sal = self._patch_dispatch_context()
        with ps as MockPS, dob, sal:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.riverty._buckaroo_create_payment(tx, client)

        mock_builder.authorize.assert_called_once()
        mock_builder.pay.assert_not_called()

    def test_create_payment_raises_when_no_articles(self):
        """Riverty without sale_order_ids → ValidationError before SDK call.

        Beats letting Riverty 400 mid-checkout; merchant gets a clear
        signal that the cart is the problem.
        """
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.riverty.id,
                "reference": "TX-RV-NOART",
                "amount": 50.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )
        client = MagicMock()
        mock_builder, _mock_response = make_mock_sdk_builder()
        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_riverty.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            with self.assertRaises(ValidationError) as ctx:
                self.riverty._buckaroo_create_payment(tx, client)
        self.assertIn("articles", str(ctx.exception))
        # Builder must not have been built when we fail-fast on articles.
        mock_builder.pay.assert_not_called()
        mock_builder.authorize.assert_not_called()

    def test_create_payment_propagates_resolve_birthdate_raise(self):
        """``resolve_birthdate(missing_error=...)`` raises when both
        session and partner are empty; ``_buckaroo_create_payment``
        propagates that error verbatim instead of swallowing it."""
        tx = self._make_tx_with_product(reference="TX-RV-NODOB")
        client = MagicMock()
        mock_builder, _mock_response = make_mock_sdk_builder()
        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_riverty.PaymentService"
            ) as MockPS,
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_riverty.resolve_birthdate",
                side_effect=ValidationError("Please provide a date of birth"),
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            with self.assertRaises(ValidationError) as ctx:
                self.riverty._buckaroo_create_payment(tx, client)
        self.assertIn("date of birth", str(ctx.exception))
        mock_builder.pay.assert_not_called()

    def test_create_payment_raises_when_nl_salutation_missing(self):
        """NL/BE customers MUST send Salutation (Mr/Mrs/Miss) per
        Riverty docs — surface as a merchant-friendly error rather than
        wait for Riverty to 491."""
        # Clear the partner-level fallback so the empty session value
        # passed via the patch isn't shadowed.
        self.partner_nl.buckaroo_riverty_salutation = False
        tx = self._make_tx_with_product(reference="TX-RV-NOSAL")
        client = MagicMock()
        mock_builder, _mock_response = make_mock_sdk_builder()
        ps, dob, sal = self._patch_dispatch_context(salutation="")
        with ps as MockPS, dob, sal:
            MockPS.return_value.create_payment.return_value = mock_builder
            with self.assertRaises(ValidationError) as ctx:
                self.riverty._buckaroo_create_payment(tx, client)
        self.assertIn("salutation", str(ctx.exception).lower())
        mock_builder.pay.assert_not_called()

    def test_create_payment_de_customer_no_salutation_required(self):
        """German customers don't have Salutation as a required field
        per Riverty docs; the request should proceed without one."""
        self.partner_nl.country_id = self.env.ref("base.de")
        self.partner_nl.buckaroo_riverty_salutation = False
        tx = self._make_tx_with_product(reference="TX-RV-DE")
        client = MagicMock()
        mock_builder, _mock_response = make_mock_sdk_builder()
        ps, dob, sal = self._patch_dispatch_context(salutation="")
        with ps as MockPS, dob, sal:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.riverty._buckaroo_create_payment(tx, client)
        mock_builder.pay.assert_called_once()

    def test_create_payment_uses_resolved_birthdate(self):
        """Whatever ``resolve_birthdate`` returns lands in the SDK
        ``BirthDate`` parameter unchanged."""
        tx = self._make_tx_with_product(reference="TX-RV-PARTDOB")
        client = MagicMock()
        mock_builder, _mock_response = make_mock_sdk_builder()
        ps, dob, sal = self._patch_dispatch_context(birthdate="15-05-1990")
        with ps as MockPS, dob, sal:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.riverty._buckaroo_create_payment(tx, client)

        params_by_name = {
            call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list
        }
        self.assertEqual(params_by_name["billingCustomer"][0]["BirthDate"], "15-05-1990")
        mock_builder.pay.assert_called_once()

    def test_create_payment_b2b_registry_overrides_empty_partner_value(self):
        """A checkout-supplied identification number overrides an empty
        partner ``company_registry`` and lands in ``IdentificationNumber``
        for both billing and shipping customer groups — this is the bug
        being fixed."""
        tx = self._make_tx_with_product(reference="TX-RV-B2BREG")
        client = MagicMock()
        mock_builder, _mock_response = make_mock_sdk_builder()
        b2b_data = get_customer_data(
            make_partner(
                is_company=True,
                commercial_company_name="Acme BV",
                company_registry=None,
            )
        )
        ps, dob, sal = self._patch_dispatch_context()
        with (
            ps as MockPS,
            dob,
            sal,
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_riverty."
                "resolve_bnpl_customer_data",
                return_value=(b2b_data, b2b_data, True),
            ),
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_riverty."
                "resolve_b2b_registry",
                return_value="12345678",
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            self.riverty._buckaroo_create_payment(tx, client)

        params_by_name = {
            call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list
        }
        self.assertEqual(
            params_by_name["billingCustomer"][0]["IdentificationNumber"], "12345678"
        )
        self.assertEqual(
            params_by_name["shippingCustomer"][0]["IdentificationNumber"], "12345678"
        )

    def test_create_payment_b2b_registry_not_applied_to_different_shipping_company(self):
        """When shipping is a different company than billing (separate
        legal entity), the checkout-collected registry number must only
        land on billingCustomer — shippingCustomer keeps its own
        partner-derived chamber_of_commerce."""
        tx = self._make_tx_with_product(reference="TX-RV-B2BSHIP")
        client = MagicMock()
        mock_builder, _mock_response = make_mock_sdk_builder()
        billing_data = get_customer_data(
            make_partner(
                is_company=True,
                commercial_company_name="Acme BV",
                company_registry=None,
            )
        )
        shipping_data = get_customer_data(
            make_partner(
                is_company=True,
                commercial_company_name="Other Company BV",
                company_registry="SHIP-COC-1",
            )
        )
        ps, dob, sal = self._patch_dispatch_context()
        with (
            ps as MockPS,
            dob,
            sal,
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_riverty."
                "resolve_bnpl_customer_data",
                return_value=(billing_data, shipping_data, False),
            ),
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_riverty."
                "resolve_b2b_registry",
                return_value="12345678",
            ),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            self.riverty._buckaroo_create_payment(tx, client)

        params_by_name = {
            call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list
        }
        self.assertEqual(
            params_by_name["billingCustomer"][0]["IdentificationNumber"], "12345678"
        )
        self.assertEqual(
            params_by_name["shippingCustomer"][0]["IdentificationNumber"], "SHIP-COC-1"
        )

    def test_create_payment_b2c_customer_has_no_identification_number(self):
        """B2C orders never send ``IdentificationNumber`` / ``CompanyName``,
        unchanged by the B2B registry resolution added for Riverty."""
        tx = self._make_tx_with_product(reference="TX-RV-B2C")
        client = MagicMock()
        mock_builder, _mock_response = make_mock_sdk_builder()
        ps, dob, sal = self._patch_dispatch_context()
        with ps as MockPS, dob, sal:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.riverty._buckaroo_create_payment(tx, client)

        params_by_name = {
            call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list
        }
        billing = params_by_name["billingCustomer"][0]
        self.assertNotIn("IdentificationNumber", billing)
        self.assertNotIn("CompanyName", billing)

    def test_b2c_order_still_consumes_stale_registry_session_key(self):
        """A B2C order must still resolve (and thus pop) the identification
        number session key, matching Billink's unconditional call — a
        stale key left by an earlier B2B attempt must never survive to
        leak into a later B2B order (matches Billink's behavior)."""
        tx = self._make_tx_with_product(reference="TX-RV-B2CPOP")
        client = MagicMock()
        mock_builder, _mock_response = make_mock_sdk_builder()
        ps, dob, sal = self._patch_dispatch_context()
        with (
            ps as MockPS,
            dob,
            sal,
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_riverty."
                "resolve_b2b_registry",
                return_value="",
            ) as mock_resolve,
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            self.riverty._buckaroo_create_payment(tx, client)

        mock_resolve.assert_called_once_with(tx, "buckaroo_riverty_identification_number")


@tagged("post_install", "-at_install")
class TestRivertyPaymentAction(BuckarooOfficialCommon):
    """_buckaroo_get_payment_action override for riverty."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.riverty = cls.env.ref("payment_buckaroo_official.payment_method_riverty")
        cls.buckaroo.payment_method_ids = [Command.link(cls.riverty.id)]

    def setUp(self):
        # Sibling test classes (CaptureAndVoid, AuthorizeWebhook) flip
        # this field to 'authorize' inside their own savepoint, but the
        # ORM record cache held on cls.riverty can retain the toggled
        # value across class teardown rollbacks. Force the default each
        # test so the action override returns ``None`` as expected.
        super().setUp()
        self.riverty.buckaroo_official_riverty_authorize = "pay"

    def test_default_pay_returns_super_default(self):
        """Default config (Pay) delegates to base which returns None."""
        self.assertEqual(self.riverty.buckaroo_official_riverty_authorize, "pay")
        self.assertIsNone(self.riverty._buckaroo_get_payment_action())

    def test_authorize_config_returns_authorize(self):
        self.riverty.buckaroo_official_riverty_authorize = "authorize"
        self.assertEqual(self.riverty._buckaroo_get_payment_action(), "authorize")

    def test_non_riverty_delegates_to_base(self):
        """Other methods must not be affected by the riverty override."""
        self.assertIsNone(self.ideal._buckaroo_get_payment_action())


@tagged("post_install", "-at_install")
class TestRivertyAuthorizeWebhookSetsAuthorizedState(BuckarooOfficialCommon):
    """When tx is in authorize mode, a 190 callback transitions to 'authorized'."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.riverty = cls.env.ref("payment_buckaroo_official.payment_method_riverty")
        cls.buckaroo.payment_method_ids = [Command.link(cls.riverty.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {
                "name": "Jan",
                "country_id": cls.env.ref("base.nl").id,
            }
        )

    def setUp(self):
        # Per-test write so the class teardown's savepoint rollback
        # restores the default 'pay' value for sibling test classes.
        super().setUp()
        self.riverty.buckaroo_official_riverty_authorize = "authorize"

    def test_e2e_creation_to_success_callback_sets_authorized(self):
        redirect_url = "https://testcheckout.buckaroo.nl/pay/AUTH"
        response = MagicMock()
        response.get_redirect_url.return_value = redirect_url
        response.key = "AUTH_KEY_E2E"
        response.status_code = 200
        response.redirect_url = redirect_url
        response.required_action = None
        response.buckaroo_status_message = "Success"
        response._raw_data = {}

        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.riverty.id,
                "reference": "RIVERTY-AUTH-E2E",
                "amount": 100.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )

        PaymentMethod = type(tx.payment_method_id)
        with (
            patch.object(PaymentMethod, "_buckaroo_create_payment", return_value=response),
            patch.object(
                PaymentMethod, "_buckaroo_extract_redirect_url", return_value=redirect_url
            ),
        ):
            tx._get_specific_processing_values({})

        self.assertEqual(tx.buckaroo_official_payment_action, "authorize")
        self.assertEqual(tx.provider_reference, "AUTH_KEY_E2E")

        callback = parsed_from_form(
            {
                "brq_invoicenumber": "RIVERTY-AUTH-E2E",
                "brq_amount": "100.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "AUTH_KEY_E2E",
            }
        )
        self.env["payment.transaction"].sudo()._process("buckaroo_official", callback)
        self.assertEqual(tx.state, "authorized")


@tagged("post_install", "-at_install")
class TestRivertyCaptureAndVoid(BuckarooOfficialCommon):
    """Capture/void on a Riverty authorize uses base SDK ``capture`` / ``cancelAuthorize``."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.riverty = cls.env.ref("payment_buckaroo_official.payment_method_riverty")
        cls.buckaroo.payment_method_ids = [Command.link(cls.riverty.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {
                "name": "Jan",
                "country_id": cls.env.ref("base.nl").id,
            }
        )

    def setUp(self):
        # Per-test write so the class teardown's savepoint rollback
        # restores the default 'pay' value for sibling test classes.
        super().setUp()
        self.riverty.buckaroo_official_riverty_authorize = "authorize"

    def _create_authorized_tx(self, reference="RV-AUTH", provider_reference="RV_AUTH_KEY"):
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.riverty.id,
                "reference": reference,
                "amount": 100.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )
        tx.provider_reference = provider_reference
        tx.buckaroo_official_payment_action = "authorize"
        return tx

    def test_capture_calls_sdk_capture(self):
        tx = self._create_authorized_tx(provider_reference="RV_CAP_KEY")
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.riverty._buckaroo_create_capture(tx, client)

        mock_builder.capture.assert_called_once_with(original_transaction_key="RV_CAP_KEY")

    def test_void_calls_sdk_cancel_authorize(self):
        tx = self._create_authorized_tx(provider_reference="RV_VOID_KEY")
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.riverty._buckaroo_create_void(tx, client)

        mock_builder.cancelAuthorize.assert_called_once_with(
            original_transaction_key="RV_VOID_KEY",
        )


@tagged("post_install", "-at_install")
class TestRivertyImageUrl(BuckarooOfficialCommon):
    """``_format_riverty_articles`` attaches an ``ImageUrl`` only when
    the article carries a product reference with an image attached."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.riverty = cls.env.ref("payment_buckaroo_official.payment_method_riverty")
        cls.buckaroo.payment_method_ids = [Command.link(cls.riverty.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {
                "name": "Jan",
                "country_id": cls.env.ref("base.nl").id,
            }
        )

    def _make_product(self, **vals):
        return self.env["product.product"].create(
            {
                "name": "Widget",
                "default_code": "WIDGET-IMG",
                "list_price": 25.0,
                **vals,
            }
        )

    def _format(self, article, base_url="https://shop.test"):
        return RivertyPaymentMethod._format_riverty_articles(
            [
                {
                    "identifier": "X",
                    "description": "X",
                    "quantity": 1.0,
                    "unit_price_incl": 10.0,
                    "unit_price_excl": 10.0,
                    "vat_percentage": 0.0,
                    "vat_amount": 0.0,
                    **article,
                }
            ],
            base_url,
        )[0]

    def test_url_built_for_product_with_image(self):
        # 1x1 transparent PNG seeded on ``image_1920`` (the source field on
        # product.template); ``image_1024`` is derived/related from it.
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAA"
            "AAYAAjCB0C8AAAAASUVORK5CYII="
        )
        product = self._make_product(image_1920=png_b64)
        entry = self._format({"type": "product", "product": product})
        self.assertIn("/web/image/product.product/", entry["ImageUrl"])
        self.assertIn("/image_1024/", entry["ImageUrl"])
        # Riverty's URL validator regexes on the suffix and accepts only
        # ``gif`` / ``jpeg`` / ``jpg`` / ``png`` / ``webp``. Pin the
        # ``.png`` filename slug so a future refactor that drops the
        # extension fails loudly here instead of in production.
        self.assertTrue(entry["ImageUrl"].endswith(".png"))

    def test_url_omitted_for_non_product_line(self):
        entry = self._format({"type": "rounding", "product": None})
        self.assertNotIn("ImageUrl", entry)

    def test_url_emitted_even_when_product_has_no_image(self):
        # The ``image_1024`` truthiness check would load the binary blob
        # per line; let Odoo's /web/image route serve a placeholder when
        # the field is empty.
        product = self._make_product()
        entry = self._format({"type": "product", "product": product})
        self.assertIn("/web/image/product.product/", entry["ImageUrl"])

    def test_url_omitted_when_product_missing(self):
        entry = self._format({"type": "product", "product": None})
        self.assertNotIn("ImageUrl", entry)

    def test_url_omitted_when_base_url_empty(self):
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAA"
            "AAYAAjCB0C8AAAAASUVORK5CYII="
        )
        product = self._make_product(image_1920=png_b64)
        entry = self._format({"type": "product", "product": product}, base_url="")
        self.assertNotIn("ImageUrl", entry)


class TestFormatRivertyArticles(BaseCase):
    def test_maps_generic_dict_to_riverty_pascalcase(self):
        generic = [
            {
                "identifier": "SKU-1",
                "description": "Widget",
                "quantity": 2.0,
                "unit_price_incl": 12.10,
                "unit_price_excl": 10.0,
                "vat_percentage": 21.0,
                "vat_amount": 4.20,
                "type": "product",
            }
        ]
        result = RivertyPaymentMethod._format_riverty_articles(generic)
        self.assertEqual(len(result), 1)
        a = result[0]
        self.assertEqual(a["Identifier"], "SKU-1")
        self.assertEqual(a["Description"], "Widget")
        self.assertEqual(a["Quantity"], "2")
        self.assertAlmostEqual(a["GrossUnitPrice"], 12.10, places=2)
        self.assertAlmostEqual(a["VatPercentage"], 21.0, places=2)
        # Riverty's ``Type`` enum: physical product → PhysicalArticle.
        self.assertEqual(a["Type"], "PhysicalArticle")
        self.assertNotIn("ImageUrl", a)

    def test_shipping_line_maps_to_shipping_fee_type(self):
        generic = [
            {
                "identifier": "SHIP",
                "description": "Shipping",
                "quantity": 1.0,
                "unit_price_incl": 5.0,
                "unit_price_excl": 5.0,
                "vat_percentage": 0.0,
                "vat_amount": 0.0,
                "type": "shipping",
            }
        ]
        result = RivertyPaymentMethod._format_riverty_articles(generic)
        self.assertEqual(result[0]["Type"], "ShippingFee")

    def test_rounding_line_maps_to_surcharge_type(self):
        """``rounding`` lines are appended by helpers/articles when the
        line-item sum mismatches transaction.amount; Riverty's Surcharge
        type is the closest fit and avoids miscategorising as a product."""
        generic = [
            {
                "identifier": "rounding",
                "description": "Rounding correction",
                "quantity": 1.0,
                "unit_price_incl": 0.01,
                "unit_price_excl": 0.01,
                "vat_percentage": 0.0,
                "vat_amount": 0.0,
                "type": "rounding",
            }
        ]
        result = RivertyPaymentMethod._format_riverty_articles(generic)
        self.assertEqual(result[0]["Type"], "Surcharge")

    def test_fractional_quantity_rounds_to_nearest_int(self):
        """Riverty's ``Quantity`` is wire-typed as integer. A weighted
        line of 1.5 kg rounds to 2 — banker's rounding (Python ``round``)."""
        generic = [
            {
                "identifier": "KG",
                "description": "Cheese",
                "quantity": 1.5,
                "unit_price_incl": 10.0,
                "unit_price_excl": 10.0,
                "vat_percentage": 0.0,
                "vat_amount": 0.0,
                "type": "product",
            }
        ]
        result = RivertyPaymentMethod._format_riverty_articles(generic)
        self.assertEqual(result[0]["Quantity"], "2")

    def test_sub_unit_quantity_clamps_to_one(self):
        """A 0.4 quantity rounds to 0; clamping to 1 prevents Riverty
        from receiving a free-article line that the customer paid for."""
        generic = [
            {
                "identifier": "SMALL",
                "description": "Small Sample",
                "quantity": 0.4,
                "unit_price_incl": 10.0,
                "unit_price_excl": 10.0,
                "vat_percentage": 0.0,
                "vat_amount": 0.0,
                "type": "product",
            }
        ]
        result = RivertyPaymentMethod._format_riverty_articles(generic)
        self.assertEqual(result[0]["Quantity"], "1")

    def test_image_url_omitted_when_no_product_reference(self):
        generic = [
            {
                "identifier": "SKU-NOIMG",
                "description": "Widget",
                "quantity": 1.0,
                "unit_price_incl": 10.0,
                "unit_price_excl": 10.0,
                "vat_percentage": 0.0,
                "vat_amount": 0.0,
                "type": "product",
                "product": None,
            }
        ]
        result = RivertyPaymentMethod._format_riverty_articles(generic, "https://shop.test")
        self.assertNotIn("ImageUrl", result[0])

    def test_maps_multiple_articles_keep_order(self):
        generic = [
            {
                "identifier": "A",
                "description": "A",
                "quantity": 1.0,
                "unit_price_incl": 10.0,
                "unit_price_excl": 10.0,
                "vat_percentage": 0.0,
                "vat_amount": 0.0,
                "type": "product",
            },
            {
                "identifier": "B",
                "description": "B",
                "quantity": 3.0,
                "unit_price_incl": 5.0,
                "unit_price_excl": 5.0,
                "vat_percentage": 0.0,
                "vat_amount": 0.0,
                "type": "product",
            },
        ]
        result = RivertyPaymentMethod._format_riverty_articles(generic)
        self.assertEqual([a["Identifier"] for a in result], ["A", "B"])
        self.assertEqual(result[1]["Quantity"], "3")


class TestFormatRivertyCustomer(BaseCase):
    def test_b2c_customer_uses_person_category(self):
        data = get_customer_data(
            make_partner(
                name="Jan de Vries",
                street="Keizersgracht 424",
                zip_code="1016 GC",
                city="Amsterdam",
                country_code="NL",
                email="jan@example.com",
                phone="+31612345678",
            )
        )
        result = RivertyPaymentMethod._format_riverty_customer(data)
        self.assertEqual(result["Category"], "Person")
        self.assertEqual(result["FirstName"], "Jan")
        self.assertEqual(result["LastName"], "de Vries")
        self.assertEqual(result["Street"], "Keizersgracht")
        # House number split: 424 → StreetNumber=424, no suffix.
        self.assertEqual(result["StreetNumber"], "424")
        self.assertEqual(result["StreetNumberAdditional"], "")
        self.assertEqual(result["PostalCode"], "1016 GC")
        self.assertEqual(result["City"], "Amsterdam")
        self.assertEqual(result["Country"], "NL")
        self.assertEqual(result["Email"], "jan@example.com")
        # Phone sanitized to digits-only — Riverty rejects ``+`` / spaces.
        self.assertEqual(result["MobilePhone"], "31612345678")
        self.assertEqual(result["Phone"], "31612345678")
        # The formatter returns base customer fields only — BirthDate and
        # Salutation are added post-call by ``_buckaroo_create_payment``
        # (matches Billink's pattern).
        self.assertNotIn("BirthDate", result)
        self.assertNotIn("Salutation", result)
        # B2C does not include CompanyName / IdentificationNumber.
        self.assertNotIn("CompanyName", result)
        self.assertNotIn("IdentificationNumber", result)

    def test_house_number_with_suffix_splits_into_additional(self):
        partner = make_partner(street="Hoofdstraat 90A", street2=None)
        data = get_customer_data(partner)
        result = RivertyPaymentMethod._format_riverty_customer(data)
        self.assertEqual(result["Street"], "Hoofdstraat")
        self.assertEqual(result["StreetNumber"], "90")
        self.assertEqual(result["StreetNumberAdditional"], "A")

    def test_phone_sanitized_strips_plus_and_whitespace(self):
        data = get_customer_data(make_partner(phone="+31 20 123 4567"))
        result = RivertyPaymentMethod._format_riverty_customer(data)
        self.assertEqual(result["MobilePhone"], "31201234567")
        self.assertEqual(result["Phone"], "31201234567")

    def test_b2b_customer_has_company_fields(self):
        data = get_customer_data(
            make_partner(
                is_company=True,
                commercial_company_name="Acme BV",
                company_registry="12345678",
                vat="NL123456789B01",
            )
        )
        result = RivertyPaymentMethod._format_riverty_customer(data)
        self.assertEqual(result["Category"], "Company")
        # B2B uses CompanyName for the trading name and
        # IdentificationNumber for the chamber-of-commerce / VAT id; the
        # B2C ``CareOf`` field is NOT used as a company-name carrier.
        self.assertEqual(result["CompanyName"], "Acme BV")
        self.assertEqual(result["IdentificationNumber"], "12345678")

    def test_b2b_customer_omits_identification_number_when_empty(self):
        """No registry number anywhere (partner nor checkout-supplied) —
        Riverty must not receive an empty ``IdentificationNumber``."""
        data = get_customer_data(
            make_partner(
                is_company=True,
                commercial_company_name="Acme BV",
                company_registry=None,
            )
        )
        result = RivertyPaymentMethod._format_riverty_customer(data)
        self.assertEqual(result["Category"], "Company")
        self.assertEqual(result["CompanyName"], "Acme BV")
        self.assertNotIn("IdentificationNumber", result)


@tagged("post_install", "-at_install")
class TestRivertyNoRedirectSettlement(BuckarooOfficialCommon):
    """A Riverty ``pay``/``authorize`` can be approved synchronously and return
    no redirect URL; ``_buckaroo_handle_no_redirect_response`` must settle the
    tx from the sync response and route via ``/shop/payment/validate`` instead
    of letting the generic flow treat it as a failure."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.riverty = cls.env.ref("payment_buckaroo_official.payment_method_riverty")
        cls.buckaroo.payment_method_ids = [Command.link(cls.riverty.id)]

    def test_success_pay_sets_done_and_routes_to_payment_status(self):
        tx = self._create_buckaroo_tx(reference="TX-RIV-NR", payment_method=self.riverty)
        response = make_mock_sdk_response(190)

        result = self.riverty._buckaroo_handle_no_redirect_response(tx, response)

        # Success routes to /payment/status so the poll post-processes the tx
        # in a fresh request and the order confirms now, not on the cron.
        self.assertTrue(result["api_url"].endswith("/payment/status"))
        self.assertEqual(tx.provider_reference, response.key)
        self.assertEqual(tx.state, "done")

    def test_success_authorize_sets_authorized_and_routes_to_payment_status(self):
        tx = self._create_buckaroo_tx(reference="TX-RIV-NR-AUTH", payment_method=self.riverty)
        tx.buckaroo_official_payment_action = "authorize"
        response = make_mock_sdk_response(190)

        result = self.riverty._buckaroo_handle_no_redirect_response(tx, response)

        self.assertTrue(result["api_url"].endswith("/payment/status"))
        self.assertEqual(tx.state, "authorized")

    def test_pending_sets_pending_and_routes_to_validate(self):
        tx = self._create_buckaroo_tx(reference="TX-RIV-NR-PENDING", payment_method=self.riverty)
        response = make_mock_sdk_response(791)

        result = self.riverty._buckaroo_handle_no_redirect_response(tx, response)

        self.assertTrue(result["api_url"].endswith("/shop/payment/validate"))
        self.assertEqual(tx.provider_reference, response.key)
        self.assertEqual(tx.state, "pending")

    def test_failure_returns_none_to_fall_through_to_error_path(self):
        tx = self._create_buckaroo_tx(reference="TX-RIV-NR-FAIL", payment_method=self.riverty)
        response = make_mock_sdk_response(490)

        result = self.riverty._buckaroo_handle_no_redirect_response(tx, response)

        self.assertIsNone(result)
        self.assertNotEqual(tx.state, "done")

    def test_non_riverty_method_delegates_to_base(self):
        tx = self._create_buckaroo_tx(reference="TX-RIV-NR-IDEAL")
        result = self.ideal._buckaroo_handle_no_redirect_response(tx, make_mock_sdk_response(190))
        self.assertIsNone(result)
