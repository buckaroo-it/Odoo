# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Push-driven capture completion (Plaza / external capture).

When a 190 Collecting push arrives for an authorize-mode tx with a key
different from the auth's provider_reference, the addon must spawn a
capture child (mirrors the admin-button path) so the auth can complete,
account.payment can be created, and the W8 refund-key resolver has a
capture key to read from. Direct-pay methods must be untouched: their
initial 190 push still sets the tx to done with no child.
"""

from unittest.mock import patch

from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon, make_mock_request, parsed_from_form


def _capture_push(reference, txn_key, amount="100.00"):
    return parsed_from_form(
        {
            "brq_invoicenumber": reference,
            "brq_amount": amount,
            "brq_currency": "EUR",
            "brq_statuscode": "190",
            "brq_transactions": txn_key,
            "brq_mutationtype": "Collecting",
            "brq_transaction_type": "C339",
        }
    )


def _route_push(env, parsed):
    """Drive the same split + process path the webhook controller runs."""
    tx_sudo = (
        env["payment.transaction"]
        .sudo()
        ._search_by_reference(
            "buckaroo_official",
            parsed,
        )
    )
    if not tx_sudo:
        return None
    remainder_tx = tx_sudo._buckaroo_split_remainder_push(parsed)
    target = remainder_tx or tx_sudo
    target._process("buckaroo_official", parsed)
    if remainder_tx and remainder_tx.state == "done":
        remainder_tx._post_process()
    return remainder_tx or tx_sudo


@tagged("post_install", "-at_install")
class TestKlarnaCapturePush(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.klarna = cls.env.ref("payment_buckaroo_official.payment_method_klarna")
        cls.buckaroo.payment_method_ids = [Command.link(cls.klarna.id)]
        cls.partner_nl = cls.env["res.partner"].create(
            {"name": "Jan", "country_id": cls.env.ref("base.nl").id}
        )

    def _create_authorized_tx(self, reference="KL-CAP-AUTH", provider_reference="KL_AUTH_KEY"):
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.klarna.id,
                "reference": reference,
                "amount": 100.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )
        tx.provider_reference = provider_reference
        tx.buckaroo_official_payment_action = "authorize"
        tx._set_authorized()
        self.assertEqual(tx.state, "authorized")
        return tx

    def test_collecting_push_after_authorize_creates_capture_child(self):
        tx = self._create_authorized_tx(reference="KL-CAP-001")
        parsed = _capture_push("KL-CAP-001", "KL_CAPTURE_KEY")
        _route_push(self.env, parsed)

        capture_child = tx.child_transaction_ids
        self.assertEqual(len(capture_child), 1)
        self.assertEqual(capture_child.state, "done")
        self.assertEqual(capture_child.provider_reference, "KL_CAPTURE_KEY")
        self.assertEqual(capture_child.source_transaction_id, tx)
        self.assertEqual(tx.state, "done")
        # W8: refund-key resolver reads the capture key, not the auth key.
        resolved = self.klarna._buckaroo_resolve_original_transaction_key(tx)
        self.assertEqual(resolved, "KL_CAPTURE_KEY")

    def test_initial_reserve_push_still_authorizes(self):
        """Regression: same-key 190 on a draft auth-mode tx must reach
        ``authorized``, not spawn a capture child."""
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.klarna.id,
                "reference": "KL-RESERVE-001",
                "amount": 100.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )
        tx.provider_reference = "KL_RES_KEY"
        tx.buckaroo_official_payment_action = "authorize"

        parsed = _capture_push("KL-RESERVE-001", "KL_RES_KEY")
        _route_push(self.env, parsed)

        self.assertEqual(tx.state, "authorized")
        self.assertFalse(tx.child_transaction_ids)

    def test_refund_push_unaffected(self):
        """Refund push lands on the refund tx, not the auth; state stays put."""
        auth = self._create_authorized_tx(reference="KL-RFND-AUTH")
        # Promote to done so a refund can be issued.
        cap_parsed = _capture_push("KL-RFND-AUTH", "KL_CAP_KEY")
        _route_push(self.env, cap_parsed)
        self.assertEqual(auth.state, "done")
        capture_child = auth.child_transaction_ids
        self.assertEqual(capture_child.state, "done")

        # Refund tx created externally (no _send_refund_request needed).
        refund_tx = auth._create_child_transaction(50.0, is_refund=True)
        refund_tx.provider_reference = "KL_REFUND_PENDING"
        refund_tx._set_pending()

        refund_push = parsed_from_form(
            {
                "brq_invoicenumber": refund_tx.reference,
                "brq_amount_credit": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "KL_REFUND_DONE",
                "brq_mutationtype": "Collecting",
            }
        )
        _route_push(self.env, refund_push)

        self.assertEqual(refund_tx.state, "done")
        # Auth + capture chain untouched.
        self.assertEqual(auth.state, "done")
        self.assertEqual(capture_child.state, "done")
        # No phantom second capture child.
        self.assertEqual(
            len(auth.child_transaction_ids.filtered(lambda t: t.operation != "refund")),
            1,
        )

    def test_duplicate_collecting_push_idempotent(self):
        tx = self._create_authorized_tx(reference="KL-DUP-001")
        parsed = _capture_push("KL-DUP-001", "KL_DUP_CAP_KEY")
        _route_push(self.env, parsed)
        _route_push(self.env, parsed)  # replay

        captures = tx.child_transaction_ids.filtered(lambda t: t.operation != "refund")
        self.assertEqual(len(captures), 1)
        self.assertEqual(tx.state, "done")

    def test_collecting_push_post_processes_capture_child(self):
        """``_post_process`` must fire on the capture child so the account
        payment is created the moment the push lands (cron-free). Driven
        through the real controller so the assertion pins ``main.py``'s
        post-process call, not the test helper's own ``_post_process``."""
        from ..controllers.main import BuckarooOfficialController  # noqa: PLC0415
        from odoo.addons.account_payment.models.payment_transaction import (  # noqa: PLC0415
            PaymentTransaction,
        )

        tx = self._create_authorized_tx(reference="KL-PP-001")
        req = make_mock_request(values=_capture_push("KL-PP-001", "KL_PP_CAP_KEY").raw)
        req.env = self.env

        posted = []
        with (
            patch("odoo.addons.payment_buckaroo_official.controllers.main.verify_signature"),
            patch.object(PaymentTransaction, "_post_process", lambda self: posted.append(self)),
        ):
            BuckarooOfficialController._handle_push(req)

        capture_child = tx.child_transaction_ids.filtered(lambda t: t.operation != "refund")
        self.assertEqual(len(capture_child), 1)
        # The framework also pings _post_process on an empty set during
        # _process; drop those. The only real record post-processed is the
        # spawned child via main.py:65 — remove that line and ``captured`` empties.
        captured = [record for record in posted if record]
        self.assertEqual(captured, [capture_child])


@tagged("post_install", "-at_install")
class TestRivertyCapturePush(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.riverty = cls.env.ref("payment_buckaroo_official.payment_method_riverty")
        cls.buckaroo.payment_method_ids = [Command.link(cls.riverty.id)]
        cls.riverty.buckaroo_official_riverty_authorize = "authorize"
        cls.partner_nl = cls.env["res.partner"].create(
            {"name": "Jan", "country_id": cls.env.ref("base.nl").id}
        )

    def test_collecting_push_after_authorize_creates_capture_child(self):
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.riverty.id,
                "reference": "RV-CAP-001",
                "amount": 100.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner_nl.id,
                "operation": "online_redirect",
            }
        )
        tx.provider_reference = "RV_AUTH_KEY"
        tx.buckaroo_official_payment_action = "authorize"
        tx._set_authorized()

        parsed = _capture_push("RV-CAP-001", "RV_CAPTURE_KEY")
        _route_push(self.env, parsed)

        capture_child = tx.child_transaction_ids
        self.assertEqual(len(capture_child), 1)
        self.assertEqual(capture_child.state, "done")
        self.assertEqual(capture_child.provider_reference, "RV_CAPTURE_KEY")
        self.assertEqual(tx.state, "done")


@tagged("post_install", "-at_install")
class TestCreditCardCapturePush(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")
        cls.buckaroo.payment_method_ids = [Command.link(cls.creditcard.id)]
        cls.creditcard.buckaroo_official_creditcard_authorize = "authorize"

    def test_collecting_push_after_authorize_creates_capture_child(self):
        tx = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.creditcard.id,
                "reference": "CC-CAP-001",
                "amount": 100.0,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
            }
        )
        tx.provider_reference = "CC_AUTH_KEY"
        tx.buckaroo_official_payment_action = "authorize"
        tx._set_authorized()

        parsed = _capture_push("CC-CAP-001", "CC_CAPTURE_KEY")
        _route_push(self.env, parsed)

        capture_child = tx.child_transaction_ids
        self.assertEqual(len(capture_child), 1)
        self.assertEqual(capture_child.state, "done")
        self.assertEqual(capture_child.provider_reference, "CC_CAPTURE_KEY")
        self.assertEqual(tx.state, "done")


@tagged("post_install", "-at_install")
class TestDirectPayCapturePush(BuckarooOfficialCommon):
    """Direct-pay methods (iDEAL etc.) must NOT spawn a capture child on a
    190 push — they have no authorize mode and the push completes the tx."""

    def test_ideal_initial_push_goes_to_done_without_child(self):
        tx = self._create_buckaroo_tx(reference="IDEAL-DP-001", payment_method=self.ideal)
        tx.provider_reference = "IDEAL_AUTH_KEY"

        parsed = _capture_push("IDEAL-DP-001", "IDEAL_DONE_KEY", amount="50.00")
        _route_push(self.env, parsed)

        self.assertEqual(tx.state, "done")
        self.assertFalse(tx.child_transaction_ids)
