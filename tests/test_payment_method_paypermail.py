# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Tests for the Pay Per Email payment method.

Covers payment creation (asserts the PaymentInvitation action is used, not a
plain Pay, with the partner email/name passed as service params) and the
no-redirect path (tx left pending, routed to ``/shop/payment/validate``).
SDK transport is mocked throughout.
"""

from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon, make_mock_sdk_builder, parsed_from_form


@tagged("post_install", "-at_install")
class TestPayPerEmailCreatePayment(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.paypermail = cls.env.ref("payment_buckaroo_official.payment_method_paypermail")
        cls.buckaroo.payment_method_ids = [Command.link(cls.paypermail.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {
                "name": "Jane Doe",
                "country_id": cls.env.ref("base.nl").id,
                "email": "jane@example.nl",
            }
        )

    def _create_tx(self, payment_method=None, reference="TX-PPE-001"):
        return self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": (payment_method or self.paypermail).id,
                "reference": reference,
                "amount": 42.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )

    def test_uses_payment_invitation_action_with_partner_params(self):
        tx = self._create_tx()
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_paypermail.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.paypermail._buckaroo_create_payment(tx, client)

        mock_builder.execute_action.assert_called_once_with("PaymentInvitation")
        mock_builder.pay.assert_not_called()
        params = {call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list}
        self.assertEqual(params["CustomerEmail"], "jane@example.nl")
        self.assertEqual(params["CustomerFirstName"], "Jane")
        self.assertEqual(params["CustomerLastName"], "Doe")
        self.assertEqual(result, mock_response)

    def test_allowed_methods_scopes_paylink_and_excludes_payperemail(self):
        # Only iDEAL + Pay Per Email enabled on the provider.
        self.buckaroo.payment_method_ids = [Command.set([self.ideal.id, self.paypermail.id])]
        tx = self._create_tx(reference="TX-PPE-ALLOWED")
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_paypermail.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.paypermail._buckaroo_create_payment(tx, client)

        params = {call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list}
        # Enabled methods are sent as the Buckaroo service code; PPE excludes itself.
        self.assertEqual(params["PaymentMethodsAllowed"], "ideal")

    def test_single_word_name_falls_back_to_first_for_last_name(self):
        # Buckaroo requires CustomerLastName; a mononym partner must not send "".
        mononym = self.env["res.partner"].create({"name": "Madonna", "email": "madonna@example.nl"})
        tx = self._create_tx(reference="TX-PPE-MONONYM")
        tx.partner_id = mononym
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_paypermail.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.paypermail._buckaroo_create_payment(tx, client)

        params = {call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list}
        self.assertEqual(params["CustomerFirstName"], "Madonna")
        self.assertEqual(params["CustomerLastName"], "Madonna")

    def test_non_paypermail_falls_through_to_super(self):
        tx = self._create_tx(payment_method=self.ideal, reference="TX-PPE-IDEAL")
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_payment(tx, client)

        mock_builder.pay.assert_called_once()
        mock_builder.execute_action.assert_not_called()
        self.assertEqual(result, mock_response)


@tagged("post_install", "-at_install")
class TestPayPerEmailNoRedirect(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.paypermail = cls.env.ref("payment_buckaroo_official.payment_method_paypermail")
        cls.buckaroo.payment_method_ids = [Command.link(cls.paypermail.id)]

    def _create_tx(self, reference="TX-PPE-PENDING"):
        return self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.paypermail.id,
                "reference": reference,
                "amount": 42.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
            }
        )

    def _pending_response(self, key="PPE_PENDING_KEY"):
        response = MagicMock()
        response.get_redirect_url.return_value = None
        response.required_action = None
        response.key = key
        response.status_code = 200
        response.redirect_url = None
        response._raw_data = {}
        response.is_pending.return_value = True
        return response

    def test_handler_sets_pending_and_routes_to_validate(self):
        tx = self._create_tx()
        result = self.paypermail._buckaroo_handle_no_redirect_response(tx, self._pending_response())

        self.assertTrue(result["api_url"].endswith("/shop/payment/validate"))
        self.assertEqual(tx.state, "pending")
        self.assertEqual(tx.provider_reference, "PPE_PENDING_KEY")

    def test_processing_flow_pending_without_redirect(self):
        response = self._pending_response(key="PPE_FLOW_KEY")
        tx = self._create_tx(reference="TX-PPE-FLOW")
        PaymentMethod = type(tx.payment_method_id)

        with (
            patch.object(PaymentMethod, "_buckaroo_create_payment", return_value=response),
            patch.object(PaymentMethod, "_buckaroo_extract_redirect_url", return_value=None),
        ):
            result = tx._get_specific_processing_values({})

        self.assertTrue(result["api_url"].endswith("/shop/payment/validate"))
        self.assertEqual(tx.state, "pending")
        self.assertEqual(tx.provider_reference, "PPE_FLOW_KEY")


@tagged("post_install", "-at_install")
class TestPayPerEmailGender(BuckarooOfficialCommon):
    """Gender capture: ``CustomerGender`` on the PaymentInvitation.

    Buckaroo requires CustomerGender, so it is always sent: the shopper's pick
    (1=Male, 2=Female) wins, falling back to the partner's saved value, then to
    0 (Unknown) when neither is set — the checkout select never hard-fails.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.paypermail = cls.env.ref("payment_buckaroo_official.payment_method_paypermail")
        cls.buckaroo.payment_method_ids = [Command.link(cls.paypermail.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {
                "name": "Jane Doe",
                "country_id": cls.env.ref("base.nl").id,
                "email": "jane@example.nl",
            }
        )

    def _create_tx(self, reference="TX-PPE-GENDER"):
        return self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.paypermail.id,
                "reference": reference,
                "amount": 42.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )

    def _create_payment(self, session=None):
        tx = self._create_tx()
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()
        fake_request = MagicMock()
        fake_request.session = session if session is not None else {}
        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_paypermail.PaymentService"
            ) as MockPS,
            patch("odoo.http.request", fake_request),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            self.paypermail._buckaroo_create_payment(tx, client)
        return {call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list}, (
            mock_builder
        )

    def test_gender_from_session_reaches_customergender_param(self):
        params, _builder = self._create_payment(session={"buckaroo_paypermail_gender": "2"})
        self.assertEqual(params["CustomerGender"], 2)

    def test_no_gender_defaults_to_unknown_and_still_sends_invitation(self):
        params, builder = self._create_payment(session={})
        self.assertEqual(params["CustomerGender"], 0)
        builder.execute_action.assert_called_once_with("PaymentInvitation")

    def test_gender_falls_back_to_partner_when_session_empty(self):
        self.partner_nl.buckaroo_paypermail_gender = "1"
        params, _builder = self._create_payment(session={})
        self.assertEqual(params["CustomerGender"], 1)


@tagged("post_install", "-at_install")
class TestPayPerEmailGenderController(BuckarooOfficialCommon):
    """Controller-level capture of ``paypermail_gender`` into the HTTP session.

    Gender is optional: a missing value must pass through cleanly, never raise.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.paypermail = cls.env.ref("payment_buckaroo_official.payment_method_paypermail")
        cls.buckaroo.payment_method_ids = [Command.link(cls.paypermail.id)]

    def _call_controller(self, **kwargs):
        import odoo.http
        from ..controllers.paypermail import PayPerEmailPaymentPortal

        portal = PayPerEmailPaymentPortal()
        fake_req = MagicMock()
        fake_req.env = self.env
        fake_req.session = {}
        with (
            patch(
                "odoo.addons.website_sale.controllers.payment.PaymentPortal.shop_payment_transaction",
                return_value="SUPER_OK",
            ) as super_stub,
            patch.object(odoo.http, "request", fake_req),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.paypermail.request",
                new=fake_req,
            ),
        ):
            result = portal.shop_payment_transaction(
                order_id=1,
                access_token="tok",
                **kwargs,
            )
            return result, super_stub, fake_req

    def test_valid_gender_stored_and_popped_before_super(self):
        result, super_stub, fake_req = self._call_controller(
            payment_method_id=self.paypermail.id,
            paypermail_gender="2",
        )
        self.assertEqual(result, "SUPER_OK")
        self.assertEqual(fake_req.session.get("buckaroo_paypermail_gender"), "2")
        _, kwargs_to_super = super_stub.call_args
        self.assertNotIn("paypermail_gender", kwargs_to_super)

    def test_missing_gender_passes_through_without_error(self):
        result, super_stub, fake_req = self._call_controller(
            payment_method_id=self.paypermail.id,
        )
        self.assertEqual(result, "SUPER_OK")
        self.assertNotIn("buckaroo_paypermail_gender", fake_req.session)

    def test_invalid_gender_ignored_without_error(self):
        result, _super_stub, fake_req = self._call_controller(
            payment_method_id=self.paypermail.id,
            paypermail_gender="not-a-number",
        )
        self.assertEqual(result, "SUPER_OK")
        self.assertNotIn("buckaroo_paypermail_gender", fake_req.session)

    def test_non_paypermail_method_passes_through(self):
        result, _super_stub, fake_req = self._call_controller(
            payment_method_id=self.ideal.id,
        )
        self.assertEqual(result, "SUPER_OK")
        self.assertNotIn("buckaroo_paypermail_gender", fake_req.session)

    def test_valid_gender_persists_on_logged_in_user_partner(self):
        partner = self.env.user.partner_id
        partner.buckaroo_paypermail_gender = False
        self._call_controller(
            payment_method_id=self.paypermail.id,
            paypermail_gender="1",
        )
        self.assertEqual(partner.buckaroo_paypermail_gender, "1")

    def test_gender_flows_param_to_session_to_builder(self):
        """End-to-end: JS route param -> controller session -> SDK
        ``CustomerGender`` parameter, with the session value consumed."""
        import odoo.http
        from ..controllers.paypermail import PayPerEmailPaymentPortal

        fake_req = MagicMock()
        fake_req.env = self.env
        fake_req.session = {}

        portal = PayPerEmailPaymentPortal()
        with (
            patch(
                "odoo.addons.website_sale.controllers.payment.PaymentPortal.shop_payment_transaction",
                return_value="SUPER_OK",
            ),
            patch.object(odoo.http, "request", fake_req),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.paypermail.request",
                new=fake_req,
            ),
        ):
            portal.shop_payment_transaction(
                order_id=1,
                access_token="tok",
                payment_method_id=self.paypermail.id,
                paypermail_gender="2",
            )
        self.assertEqual(fake_req.session["buckaroo_paypermail_gender"], "2")

        partner = self.env["res.partner"].create({"name": "Chain", "email": "c@e.nl"})
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.paypermail.id,
                "reference": "TX-PPE-CHAIN",
                "amount": 42.0,
                "currency_id": self.currency_euro.id,
                "partner_id": partner.id,
                "operation": "online_redirect",
            }
        )
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()
        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_paypermail.PaymentService"
            ) as MockPS,
            patch("odoo.http.request", fake_req),
        ):
            MockPS.return_value.create_payment.return_value = mock_builder
            self.paypermail._buckaroo_create_payment(tx, client)

        params = {call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list}
        self.assertEqual(params["CustomerGender"], 2)
        self.assertNotIn("buckaroo_paypermail_gender", fake_req.session)


@tagged("post_install", "-at_install")
class TestPayPerEmailPush(BuckarooOfficialCommon):
    """The shopper pays the emailed invitation with a real method (iDEAL,
    card, ...). The completion push names that method and carries every
    involved transaction key (comma-joined); the addon must complete the
    pending tx and reflect the actual method rather than 'payperemail'.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.paypermail = cls.env.ref("payment_buckaroo_official.payment_method_paypermail")
        cls.buckaroo.payment_method_ids = [Command.link(cls.paypermail.id)]

    def _create_pending_tx(self, reference="TX-PPE-PUSH"):
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.paypermail.id,
                "reference": reference,
                "amount": 42.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
            }
        )
        tx.provider_reference = "PPE_INVITE_KEY"
        tx._set_pending()
        return tx

    def test_success_push_completes_and_records_actual_method(self):
        tx = self._create_pending_tx(reference="TX-PPE-PUSH-001")
        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-PPE-PUSH-001",
                "brq_amount": "42.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "PPE_INVITE_KEY,IDEAL_PAID_KEY",
                "brq_transaction_method": "ideal",
            }
        )
        tx._process("buckaroo_official", parsed)

        self.assertEqual(tx.state, "done")
        # Actual method recorded, distinct from the 'payperemail' wrapper.
        self.assertEqual(tx.buckaroo_official_service_code, "ideal")
        # provider_reference is the actual payment key (last of the comma
        # list), not the invitation key, so a later refund targets it.
        self.assertEqual(tx.provider_reference, "IDEAL_PAID_KEY")

    def test_pending_push_does_not_record_payperemail_as_method(self):
        """Before the shopper picks a method the push reports 'payperemail';
        that wrapper must never be stored as the paid method."""
        tx = self._create_pending_tx(reference="TX-PPE-PUSH-PENDING")
        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-PPE-PUSH-PENDING",
                "brq_amount": "42.00",
                "brq_currency": "EUR",
                "brq_statuscode": "790",
                "brq_transactions": "PPE_INVITE_KEY",
                "brq_transaction_method": "payperemail",
            }
        )
        tx._process("buckaroo_official", parsed)

        self.assertEqual(tx.state, "pending")
        self.assertFalse(tx.buckaroo_official_service_code)

    def test_failed_push_naming_real_method_does_not_relabel(self):
        """A non-success push can still name a real method; the relabel must
        only fire on a successful payment, never on a failure."""
        tx = self._create_pending_tx(reference="TX-PPE-PUSH-FAIL")
        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-PPE-PUSH-FAIL",
                "brq_amount": "42.00",
                "brq_currency": "EUR",
                "brq_statuscode": "490",
                "brq_transactions": "PPE_INVITE_KEY,IDEAL_FAILED_KEY",
                "brq_transaction_method": "ideal",
            }
        )
        tx._process("buckaroo_official", parsed)

        self.assertNotEqual(tx.state, "done")
        self.assertFalse(tx.buckaroo_official_service_code)
        # The reference must stay the invitation key, not the failed payment leg.
        self.assertEqual(tx.provider_reference, "PPE_INVITE_KEY")


@tagged("post_install", "-at_install")
class TestPayPerEmailRefund(BuckarooOfficialCommon):
    """Refunding a completed PPE tx must route through the actually-paid
    method's service (e.g. 'ideal'), not 'payperemail' — the PPE service only
    supports PaymentInvitation, so a refund keyed to it is rejected by Buckaroo.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.paypermail = cls.env.ref("payment_buckaroo_official.payment_method_paypermail")
        cls.buckaroo.payment_method_ids = [Command.link(cls.paypermail.id)]

    def _make_source_and_refund(self, suffix, service_code="ideal"):
        source_tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.paypermail.id,
                "reference": f"SRC-PPE-{suffix}",
                "amount": 42.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
            }
        )
        source_tx.provider_reference = f"IDEAL_PAID_{suffix}"
        source_tx.buckaroo_official_service_code = service_code
        refund_tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.paypermail.id,
                "reference": f"REF-PPE-{suffix}",
                "amount": -10.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner.id,
                "operation": "refund",
            }
        )
        return source_tx, refund_tx

    def test_partial_refund_uses_actual_paid_method_service(self):
        source_tx, refund_tx = self._make_source_and_refund("001")
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_paypermail.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.paypermail._buckaroo_create_refund(source_tx, refund_tx, client)

        service_name = MockPS.return_value.create_payment.call_args[0][0]
        params = MockPS.return_value.create_payment.call_args[0][1]
        self.assertEqual(service_name, "ideal")
        self.assertEqual(params["original_transaction_key"], "IDEAL_PAID_001")
        self.assertEqual(params["refund_amount"], 10.0)
        mock_builder.refund.assert_called_once()
        self.assertEqual(result, mock_response)

    def test_refund_without_recorded_method_raises(self):
        source_tx, refund_tx = self._make_source_and_refund("NOSVC", service_code=False)

        with self.assertRaises(ValidationError):
            with patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method_paypermail.PaymentService"
            ):
                self.paypermail._buckaroo_create_refund(source_tx, refund_tx, MagicMock())
