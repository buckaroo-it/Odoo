# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Tests for the giftcard payment method branch on payment.method.

After the rework, giftcard follows the credit-card pattern: a parent
``giftcard`` method with brand children (vvvgiftcard, fashioncheque,
boekenbon, webshopgiftcard, yourgift) linked via
``primary_payment_method_id``.
"""

from unittest.mock import MagicMock, patch

from buckaroo.http.client import BuckarooApiError

from odoo.addons.account_payment.models.payment_transaction import PaymentTransaction
from odoo.addons.payment_buckaroo_official.controllers.giftcard import GiftcardPaymentPortal
from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import tagged

from .common import (
    BuckarooOfficialCommon,
    make_mock_sdk_builder,
    parsed_from_form,
    parsed_from_json,
)


def _route_giftcard_push(env, parsed):
    """Drive the same split + process path the webhook controller runs."""
    tx_sudo = env["payment.transaction"].sudo()._search_by_reference("buckaroo_official", parsed)
    if not tx_sudo:
        return None
    remainder_tx = tx_sudo._buckaroo_split_remainder_push(parsed)
    target = remainder_tx or tx_sudo
    target._process("buckaroo_official", parsed)
    if remainder_tx and remainder_tx.state == "done":
        remainder_tx._post_process()
    return remainder_tx or tx_sudo


BRAND_CODES = (
    "vvvgiftcard",
    "fashioncheque",
    "boekenbon",
    "webshopgiftcard",
    "yourgift",
)


class _GiftcardTestBase(BuckarooOfficialCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.giftcard = cls.env.ref("payment_buckaroo_official.payment_method_giftcard")
        cls.brand_vvv = cls.env.ref("payment_buckaroo_official.payment_method_brand_vvvgiftcard")
        cls.brand_fashioncheque = cls.env.ref(
            "payment_buckaroo_official.payment_method_brand_fashioncheque"
        )
        cls.brand_boekenbon = cls.env.ref(
            "payment_buckaroo_official.payment_method_brand_boekenbon"
        )
        cls.brand_webshop = cls.env.ref(
            "payment_buckaroo_official.payment_method_brand_webshopgiftcard"
        )
        cls.brand_yourgift = cls.env.ref("payment_buckaroo_official.payment_method_brand_yourgift")
        cls.buckaroo.payment_method_ids = [
            Command.link(cls.giftcard.id),
            Command.link(cls.brand_vvv.id),
            Command.link(cls.brand_fashioncheque.id),
            Command.link(cls.brand_boekenbon.id),
            Command.link(cls.brand_webshop.id),
            Command.link(cls.brand_yourgift.id),
        ]
        product = cls.env["product.product"].create(
            {"name": "Buckaroo Test Product", "list_price": 100.0, "taxes_id": [Command.clear()]}
        )
        cls.order = cls.env["sale.order"].create(
            {
                "partner_id": cls.partner.id,
                "order_line": [
                    Command.create(
                        {"product_id": product.id, "product_uom_qty": 1, "price_unit": 100.0}
                    )
                ],
            }
        )
        cls.order_total = cls.order.amount_total

    def _gc_tx(self, reference, amount, payment_method=None, source=None):
        vals = {
            "provider_id": self.buckaroo.id,
            "payment_method_id": (payment_method or self.giftcard).id,
            "reference": reference,
            "amount": amount,
            "currency_id": self.order.currency_id.id,
            "partner_id": self.partner.id,
            "operation": "online_redirect",
            "sale_order_ids": [Command.link(self.order.id)],
        }
        if source:
            vals["source_transaction_id"] = source.id
        return self.env["payment.transaction"].create(vals)


@tagged("post_install", "-at_install")
class TestGiftcardBrandRecords(_GiftcardTestBase):
    """5 brand records exist, all linked to the giftcard parent."""

    def test_five_brand_children_exist(self):
        children = self.env["payment.method"].search(
            [("primary_payment_method_id", "=", self.giftcard.id)]
        )
        self.assertEqual(len(children), 5)

    def test_brand_codes_match_expected_set(self):
        children = self.env["payment.method"].search(
            [("primary_payment_method_id", "=", self.giftcard.id)]
        )
        self.assertEqual(
            set(children.mapped("code")),
            {f"buckaroo_{code}" for code in BRAND_CODES},
        )

    def test_each_brand_has_sdk_service_name_matching_code(self):
        children = self.env["payment.method"].search(
            [("primary_payment_method_id", "=", self.giftcard.id)]
        )
        for child in children:
            with self.subTest(brand=child.code):
                self.assertEqual(
                    child.buckaroo_official_sdk_service_name,
                    child.code.removeprefix("buckaroo_"),
                )


@tagged("post_install", "-at_install")
class TestGiftcardParentRedirect(_GiftcardTestBase):
    """Parent + redirect: ``services_selectable_by_client`` lists brand codes
    *and* every other active top-level Buckaroo method with an SDK service
    name, so the shopper can pay the remainder with a real method."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.creditcard = cls.env.ref("payment_buckaroo_official.payment_method_creditcard")
        cls.googlepay = cls.env.ref("payment_buckaroo_official.payment_method_googlepay")
        cls.applepay = cls.env.ref("payment_buckaroo_official.payment_method_applepay")
        cls.bank_transfer = cls.env.ref("payment_buckaroo_official.payment_method_bank_transfer")
        cls.brand_visa = cls.env.ref("payment_buckaroo_official.payment_method_brand_visa")
        cls.brand_mastercard = cls.env.ref(
            "payment_buckaroo_official.payment_method_brand_mastercard"
        )
        cls.buckaroo.payment_method_ids = [
            Command.link(cls.creditcard.id),
            Command.link(cls.googlepay.id),
            Command.link(cls.applepay.id),
            Command.link(cls.bank_transfer.id),
            Command.link(cls.brand_visa.id),
            Command.link(cls.brand_mastercard.id),
        ]

    def _capture_selectable(self):
        self.giftcard.buckaroo_official_giftcard_method = "redirect"
        tx = self._create_buckaroo_tx(reference="TX-GC-P-001", payment_method=self.giftcard)
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_giftcard.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.giftcard._buckaroo_create_payment(tx, client)

        service_arg = MockPS.return_value.create_payment.call_args[0][0]
        params = MockPS.return_value.create_payment.call_args[0][1]
        csv = params.get("services_selectable_by_client") or ""
        return service_arg, params, csv.split(","), mock_builder

    def test_parent_redirect_selectable_services_includes_brand_codes(self):
        service_arg, params, selectable, mock_builder = self._capture_selectable()
        self.assertEqual(service_arg, "giftcards")
        self.assertNotIn("giftcard_name", params)
        self.assertEqual(params.get("continue_on_incomplete"), "1")
        for brand_code in BRAND_CODES:
            with self.subTest(brand=brand_code):
                self.assertIn(brand_code, selectable)
        mock_builder.pay_redirect.assert_called_once()

    def test_parent_redirect_selectable_services_includes_other_buckaroo_methods(self):
        _, _, selectable, _ = self._capture_selectable()
        # Mix of explicit-SDK-name methods (creditcard, googlepay, applepay,
        # bank_transfer→"transfer") and code-fallback methods (ideal, bancontact,
        # wero, eps, etc.) — all linked to the buckaroo provider must surface.
        for code in (
            "creditcard",
            "googlepay",
            "applepay",
            "transfer",
            "ideal",
            "bancontact",
            "wero",
            "eps",
            "paypal",
            "klarna",
        ):
            with self.subTest(code=code):
                self.assertIn(code, selectable)

    def test_selectable_services_reads_sdk_service_name(self):
        provider = self.buckaroo
        extra_method = self.env["payment.method"].create(
            {
                "name": "Foobar Fallback",
                "code": "foobar",
                "active": True,
                "buckaroo_official_sdk_service_name": "foobar",
            }
        )
        provider.payment_method_ids = [Command.link(extra_method.id)]
        try:
            csv = self.env["payment.method"]._buckaroo_giftcard_selectable_services(
                self.giftcard,
                provider,
            )
        finally:
            provider.payment_method_ids = [Command.unlink(extra_method.id)]
            extra_method.unlink()
        self.assertIn("foobar", csv.split(","))

    def test_parent_redirect_selectable_services_excludes_giftcard_parent(self):
        _, _, selectable, _ = self._capture_selectable()
        self.assertNotIn("giftcards", selectable)

    def test_parent_redirect_selectable_services_excludes_inactive_methods(self):
        self.creditcard.active = False
        try:
            _, _, selectable, _ = self._capture_selectable()
        finally:
            self.creditcard.active = True
        self.assertNotIn("creditcard", selectable)

    def test_parent_redirect_selectable_services_excludes_brand_children_of_other_parents(self):
        _, _, selectable, _ = self._capture_selectable()
        # creditcard parent's SDK name is included; its brand children
        # (Visa / MasterCard) must not be — they're sub-methods.
        self.assertNotIn("Visa", selectable)
        self.assertNotIn("MasterCard", selectable)

    def test_parent_redirect_selectable_services_preserves_order_brands_first(self):
        _, _, selectable, _ = self._capture_selectable()
        # The first N entries must all be giftcard brand codes (any order
        # among themselves), and fallback methods come after.
        first_block_size = len(BRAND_CODES)
        head = selectable[:first_block_size]
        tail = selectable[first_block_size:]
        self.assertEqual(set(head), set(BRAND_CODES))
        for code in ("creditcard", "googlepay", "applepay", "transfer"):
            with self.subTest(code=code):
                self.assertIn(code, tail)


@tagged("post_install", "-at_install")
class TestGiftcardBrandRedirect(_GiftcardTestBase):
    """Brand picked + redirect: SDK call sets ``giftcard_name`` to the brand code."""

    def test_brand_redirect_sets_giftcard_name_and_no_selectable_services(self):
        self.giftcard.buckaroo_official_giftcard_method = "redirect"
        tx = self._create_buckaroo_tx(reference="TX-GC-B-001", payment_method=self.brand_vvv)
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_giftcard.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.brand_vvv._buckaroo_create_payment(tx, client)

        params = MockPS.return_value.create_payment.call_args[0][1]
        self.assertEqual(params.get("giftcard_name"), "vvvgiftcard")
        self.assertNotIn("services_selectable_by_client", params)
        self.assertEqual(params.get("continue_on_incomplete"), "1")
        mock_builder.pay.assert_called_once()
        self.assertEqual(result, mock_response)
        self.assertEqual(tx.buckaroo_official_service_code, "vvvgiftcard")


@tagged("post_install", "-at_install")
class TestGiftcardBrandInline(_GiftcardTestBase):
    """Brand picked + inline mode: brand-specific Cardnumber/PIN parameter names."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.giftcard.buckaroo_official_giftcard_method = "inline"

    def _run_inline_pay(self, brand_method, cardnumber="CARD", pin="0000"):
        tx = self._create_buckaroo_tx(
            reference=f"TX-GC-{brand_method.code.upper()}-001",
            payment_method=brand_method,
        ).with_context(
            buckaroo_giftcard_cardnumber=cardnumber,
            buckaroo_giftcard_pin=pin,
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_giftcard.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            brand_method._buckaroo_create_payment(tx, client)

        params = MockPS.return_value.create_payment.call_args[0][1]
        added = {call[0][0]: call[0][1] for call in mock_builder.add_parameter.call_args_list}
        return tx, params, added

    def test_inline_uses_brand_specific_param_names(self):
        # (brand_attr, sdk_name, cardnumber, pin, card_param, pin_param)
        cases = [
            (
                "brand_fashioncheque",
                "fashioncheque",
                "FC-CARD",
                "1111",
                "FashionChequeCardNumber",
                "FashionChequePIN",
            ),
            (
                "brand_vvv",
                "vvvgiftcard",
                "VVV-CARD",
                "3333",
                "IntersolveCardnumber",
                "IntersolvePIN",
            ),
            (
                "brand_boekenbon",
                "boekenbon",
                "BB-CARD",
                "4444",
                "IntersolveCardnumber",
                "IntersolvePIN",
            ),
            (
                "brand_webshop",
                "webshopgiftcard",
                "WSG-CARD",
                "5555",
                "IntersolveCardnumber",
                "IntersolvePIN",
            ),
            (
                "brand_yourgift",
                "yourgift",
                "YG-CARD",
                "6666",
                "IntersolveCardnumber",
                "IntersolvePIN",
            ),
        ]
        for brand_attr, sdk_name, cardnumber, pin, card_param, pin_param in cases:
            with self.subTest(brand=sdk_name):
                tx, params, added = self._run_inline_pay(
                    getattr(self, brand_attr),
                    cardnumber,
                    pin,
                )
                self.assertEqual(params.get("giftcard_name"), sdk_name)
                self.assertEqual(added.get(card_param), cardnumber)
                self.assertEqual(added.get(pin_param), pin)
                self.assertNotIn("Cardnumber", added)
                self.assertNotIn("PIN", added)
                self.assertEqual(tx.buckaroo_official_service_code, sdk_name)


@tagged("post_install", "-at_install")
class TestGiftcardInlineMissingSession(_GiftcardTestBase):
    """Inline mode + brand picked + missing session keys → ValidationError."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.giftcard.buckaroo_official_giftcard_method = "inline"

    def test_inline_missing_keys_raises_validation_error(self):
        tx = self._create_buckaroo_tx(reference="TX-GC-MISS-001", payment_method=self.brand_vvv)
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_giftcard.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            with self.assertRaises(ValidationError):
                self.brand_vvv._buckaroo_create_payment(tx, client)


def _make_partial_response(
    consumed=None,
    remainder=None,
    amount_debit_root=None,
    key="GC_PARTIAL_KEY",
    remainder_url="https://checkout.buckaroo.nl/remainder/abc123",
    is_successful=True,
    group_key=None,
    related_key=None,
):
    """Build a mock SDK response that mimics Buckaroo's giftcard partial-pay shape.

    The gateway returns 190 SUCCESS for the giftcard slice, sets
    ``required_action.redirect_url`` to the hosted remainder picker, and
    exposes the consumed amount through service parameters (``AmountDebit``
    on the giftcard service entry — the request-level ``AmountDebit`` mirrors
    what was asked for, not what was actually drawn). ``amount_debit_root``
    simulates the SDK's root-level ``response.amount_debit`` (JSON
    ``AmountDebit``); some Buckaroo backends fill it with the actually-drawn
    slice on partial pay, others mirror the requested amount — the consumer
    decides via a "less than tx.amount" guard.
    """
    from odoo.addons.payment_buckaroo_official.utils import const  # noqa: PLC0415

    response = MagicMock()
    response.key = key
    response.status_code = 200
    response.get_redirect_url.return_value = None
    response.redirect_url = None
    response.required_action = MagicMock()
    response.required_action.redirect_url = remainder_url
    response.required_action.pay_remainder_details = (
        {"GroupTransaction": group_key} if group_key else None
    )
    response.related_transactions = (
        [{"RelatedTransactionKey": related_key}] if related_key else None
    )
    response.amount_debit = amount_debit_root
    response.is_successful.return_value = is_successful

    mock_sc = MagicMock()
    mock_sc.code = const.BuckarooStatusCode.SUCCESS
    mock_st = MagicMock()
    mock_st.code = mock_sc
    response.status = mock_st

    def _get_service_parameter(name):
        lname = (name or "").lower()
        if lname == "amountdebit":
            return consumed
        if lname == "remainderamount":
            return remainder
        return None

    response.get_service_parameter.side_effect = _get_service_parameter
    return response


@tagged("post_install", "-at_install")
class TestGiftcardInlinePartial(_GiftcardTestBase):
    """Inline mode + partial pay: record the consumed slice, capture the group
    transaction key, and bounce the shopper to ``/shop/payment`` so Odoo drives
    the remainder on-site. The remainder leg settles into the giftcard group via
    a PayRemainder against the captured key (see ``TestGiftcardGroupRemainder``).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.giftcard.buckaroo_official_giftcard_method = "inline"

    def _run_inline_partial(self, response, reference="TX-GC-PART-001", amount=10.00):
        tx = self._create_buckaroo_tx(
            reference=reference,
            amount=amount,
            payment_method=self.brand_vvv,
        ).with_context(
            buckaroo_giftcard_cardnumber="VVV-PART-001",
            buckaroo_giftcard_pin="1234",
        )

        mock_builder = MagicMock()
        mock_builder.pay.return_value = response

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_giftcard.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = tx._get_specific_processing_values({})
        return tx, result

    def test_inline_partial_consumes_partial_amount_and_bounces_to_shop_payment(self):
        response = _make_partial_response(consumed=5.00)
        tx, result = self._run_inline_partial(response)

        self.assertTrue(result["api_url"].endswith("/shop/payment"))
        self.assertEqual(tx.provider_reference, "GC_PARTIAL_KEY")
        self.assertEqual(tx.amount, 5.00)
        self.assertEqual(tx.state, "done")

    def test_inline_partial_uses_remainder_when_amount_debit_missing(self):
        # tx.amount = 10.00, RemainderAmount = 4.00 → consumed = 6.00
        response = _make_partial_response(consumed=None, remainder=4.00)
        tx, _ = self._run_inline_partial(response, reference="TX-GC-PART-REM-001")
        self.assertEqual(tx.amount, 6.00)
        self.assertEqual(tx.state, "done")

    def test_inline_partial_falls_back_to_root_amount_debit(self):
        # No per-service AmountDebit / RemainderAmount, but the SDK response
        # exposes root ``amount_debit`` = 5.00 strictly less than tx.amount
        # = 10.00 — that's the actually-drawn slice, not the requested.
        response = _make_partial_response(
            consumed=None,
            remainder=None,
            amount_debit_root=5.00,
        )
        tx, _ = self._run_inline_partial(response, reference="TX-GC-PART-ROOT-001")
        self.assertEqual(tx.amount, 5.00)
        self.assertEqual(tx.state, "done")

    def test_inline_partial_ignores_root_amount_debit_equal_to_tx_amount(self):
        # Root ``amount_debit`` = 10.00 equals tx.amount: that's the requested
        # amount mirrored back, not the consumed slice. Treat as unknown.
        response = _make_partial_response(
            consumed=None,
            remainder=None,
            amount_debit_root=10.00,
        )
        tx, result = self._run_inline_partial(
            response,
            reference="TX-GC-PART-ROOT-EQ-001",
        )
        # Unknown consumed -> tx left pending, shopper bounced to /shop/payment.
        self.assertTrue(result["api_url"].endswith("/shop/payment"))
        self.assertEqual(tx.state, "pending")
        # tx.amount untouched (will be set by the async push).
        self.assertEqual(tx.amount, 10.00)

    def test_inline_partial_unknown_amount_keeps_tx_pending_and_bounces(self):
        # Nothing in the sync response signals the consumed amount. Bounce
        # the shopper to /shop/payment and leave the tx pending — the
        # async push will populate the consumed amount and mark it done.
        response = _make_partial_response(consumed=None, remainder=None)
        tx = self._create_buckaroo_tx(
            reference="TX-GC-PART-UNK-001",
            payment_method=self.brand_vvv,
        ).with_context(
            buckaroo_giftcard_cardnumber="VVV-PART-001",
            buckaroo_giftcard_pin="1234",
        )
        mock_builder = MagicMock()
        mock_builder.pay.return_value = response

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_giftcard.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = tx._get_specific_processing_values({})
        self.assertTrue(result["api_url"].endswith("/shop/payment"))
        self.assertEqual(tx.state, "pending")
        self.assertEqual(tx.provider_reference, "GC_PARTIAL_KEY")

    def test_inline_partial_failure_response_not_intercepted(self):
        """Non-190 status (e.g. 490 declined) must NOT trigger the partial-pay
        bounce, regardless of ``is_successful_payment``. The hook gates on
        ``status.code.code == SUCCESS`` rather than ``response.is_successful()``
        because the SDK's ``is_successful_payment`` flag reflects the FULL
        order, not the giftcard slice — see ``test_inline_partial_intercepts_
        when_is_successful_payment_is_false_but_status_is_190`` for the
        inverse case.
        """
        # 490 + redirect_url present — not a partial-pay; framework error path
        # should run instead of our bounce.
        response = _make_partial_response(consumed=5.00)
        response.status.code.code = 490
        response.get_some_error.return_value = "Card declined"
        tx = self._create_buckaroo_tx(
            reference="TX-GC-PART-FAIL-001",
            payment_method=self.brand_vvv,
        ).with_context(
            buckaroo_giftcard_cardnumber="VVV-PART-FAIL",
            buckaroo_giftcard_pin="1234",
        )
        mock_builder = MagicMock()
        mock_builder.pay.return_value = response

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_giftcard.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            # Failure with a redirect URL is unusual; the framework's
            # redirect-URL branch runs, which currently returns the URL.
            # That's fine — our hook only activates on SUCCESS.
            result = tx._get_specific_processing_values({})
        # No partial bounce — we did NOT touch the tx amount or mark it done.
        self.assertEqual(tx.amount, 50.0)  # unchanged from default fixture
        self.assertNotEqual(tx.state, "done")
        self.assertIn("buckaroo.nl/remainder", result["api_url"])

    def test_inline_partial_intercepts_when_is_successful_payment_is_false_but_status_is_190(self):
        """Regression for BTI-974: Buckaroo sets ``is_successful_payment=false``
        on partial-pay responses because the FULL order amount wasn't settled,
        even though the giftcard slice succeeded with status_code=190. The
        hook must gate on the status code, not on
        ``response.is_successful()`` (which reads ``is_successful_payment``).
        """
        response = _make_partial_response(consumed=5.00, is_successful=False)
        # Explicit: status code stays SUCCESS (190), is_successful() returns False.
        tx, result = self._run_inline_partial(response, reference="TX-GC-BTI974-001")

        self.assertTrue(result["api_url"].endswith("/shop/payment"))
        self.assertEqual(tx.amount, 5.00)
        self.assertEqual(tx.state, "done")

    def test_inline_partial_done_tx_is_post_processed(self):
        """Regression for tx 130412/130420: inline-partial bounces to
        ``/shop/payment`` instead of ``/payment/status``, so the framework's
        status-poll route never runs and ``_post_process`` is never invoked
        — leaving ``payment_id`` NULL and hiding admin's Refund button.
        The handler must trigger post-processing explicitly after
        ``_set_done`` so account_payment's ``_post_process`` override gets
        a chance to create the ``account.payment``.
        """
        from odoo.addons.account_payment.models.payment_transaction import (  # noqa: PLC0415
            PaymentTransaction,
        )

        response = _make_partial_response(consumed=5.00)
        with patch.object(PaymentTransaction, "_post_process") as mock_pp:
            tx, _ = self._run_inline_partial(response, reference="TX-GC-PP-FIX-001")
        # sale's _post_process override calls super twice for done txs (once
        # before invoicing, once after to post the invoices), so any positive
        # call count confirms the chain ran.
        self.assertGreaterEqual(mock_pp.call_count, 1)
        self.assertEqual(tx.state, "done")

    def test_inline_partial_pending_tx_is_not_post_processed(self):
        """When the consumed amount is unknown the handler leaves the tx
        pending and bounces. Post-processing must not run for non-done txs —
        the async push will mark it done and the cron handles post-process.
        """
        from odoo.addons.account_payment.models.payment_transaction import (  # noqa: PLC0415
            PaymentTransaction,
        )

        response = _make_partial_response(consumed=None, remainder=None)
        with patch.object(PaymentTransaction, "_post_process") as mock_pp:
            tx, _ = self._run_inline_partial(response, reference="TX-GC-PP-PEND-001")
        mock_pp.assert_not_called()
        self.assertEqual(tx.state, "pending")

    def test_inline_giftcard_partial_leg_keeps_pbnk(self):
        """The giftcard slice is an independent payment — it creates its PBNK
        so it is refundable from Odoo like any other payment."""
        tx = self._gc_tx("GC-INLINE-PBNK", self.order_total - 10.0, payment_method=self.brand_vvv)
        self.assertTrue(tx._create_payment())


@tagged("post_install", "-at_install")
class TestGiftcardRedirectModePartial(_GiftcardTestBase):
    """Redirect mode keeps Buckaroo's hosted remainder picker (unchanged)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.giftcard.buckaroo_official_giftcard_method = "redirect"

    def test_redirect_mode_partial_still_uses_buckaroo_hosted_picker(self):
        # Brand picked + redirect mode + SDK returns partial-pay shape:
        # the hook must NOT intercept; the framework follows the redirect.
        response = _make_partial_response(consumed=5.00)
        tx = self._create_buckaroo_tx(
            reference="TX-GC-RP-001",
            amount=10.00,
            payment_method=self.brand_vvv,
        )

        mock_builder = MagicMock()
        mock_builder.pay.return_value = response

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_giftcard.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = tx._get_specific_processing_values({})

        # Buckaroo's hosted picker URL is followed, tx amount untouched.
        self.assertEqual(
            result["api_url"],
            "https://checkout.buckaroo.nl/remainder/abc123",
        )
        self.assertEqual(tx.amount, 10.00)
        self.assertNotEqual(tx.state, "done")


@tagged("post_install", "-at_install")
class TestGiftcardInlineBadCredentials(_GiftcardTestBase):
    """Inline mode + bad credentials: SDK raises BuckarooApiError → ValidationError."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.giftcard.buckaroo_official_giftcard_method = "inline"

    def test_inline_bad_credentials_raises_validation_error(self):
        tx = self._create_buckaroo_tx(
            reference="TX-GC-BAD-001", payment_method=self.brand_vvv
        ).with_context(
            buckaroo_giftcard_cardnumber="BAD-CARD",
            buckaroo_giftcard_pin="0000",
        )

        mock_builder = MagicMock()
        mock_builder.pay.side_effect = BuckarooApiError("Invalid card number")

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_giftcard.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            with self.assertRaises(ValidationError) as ctx:
                tx._get_specific_processing_values({})

        self.assertIn("Invalid card number", str(ctx.exception))


@tagged("post_install", "-at_install")
class TestGiftcardNoRedirectInlineSuccess(_GiftcardTestBase):
    """Inline-success no-redirect short-circuit: parent and brand both trigger."""

    def test_brand_inline_success_routes_to_payment_status(self):
        tx = self._create_buckaroo_tx(reference="TX-GC-NR-001", payment_method=self.brand_vvv)
        response = MagicMock()
        response.key = "GC_INLINE_KEY"
        response.get_redirect_url.return_value = None
        response.redirect_url = None
        response.required_action = None

        from odoo.addons.payment_buckaroo_official.utils import const  # noqa: PLC0415

        mock_sc = MagicMock()
        mock_sc.code = const.BuckarooStatusCode.SUCCESS
        mock_st = MagicMock()
        mock_st.code = mock_sc
        response.status = mock_st

        result = self.brand_vvv._buckaroo_handle_no_redirect_response(tx, response)
        self.assertIsNotNone(result)
        self.assertTrue(result["api_url"].endswith("/payment/status"))
        self.assertEqual(tx.provider_reference, "GC_INLINE_KEY")
        self.assertEqual(tx.state, "done")

    def test_no_redirect_returns_none_when_not_success(self):
        tx = self._create_buckaroo_tx(reference="TX-GC-NR-002", payment_method=self.brand_vvv)
        response = MagicMock()
        response.key = "GC_PENDING_KEY"
        response.get_redirect_url.return_value = None
        response.redirect_url = None
        response.required_action = None

        mock_sc = MagicMock()
        mock_sc.code = 790
        mock_st = MagicMock()
        mock_st.code = mock_sc
        response.status = mock_st

        result = self.brand_vvv._buckaroo_handle_no_redirect_response(tx, response)
        self.assertIsNone(result)


@tagged("post_install", "-at_install")
class TestGiftcardRefundParams(_GiftcardTestBase):
    """Refund injects ``giftcard_name`` from the source tx's service_code."""

    def _make_source_and_refund(self, suffix, brand_method, service_code):
        source_tx = self._create_buckaroo_tx(
            reference=f"SRC-GC-{suffix}",
            amount=50.0,
            payment_method=brand_method,
        )
        source_tx.provider_reference = f"SRC_KEY_{suffix}"
        source_tx.buckaroo_official_service_code = service_code
        refund_tx = self._create_buckaroo_tx(
            reference=f"REF-GC-{suffix}",
            amount=-50.0,
            payment_method=brand_method,
        )
        return source_tx, refund_tx

    def test_refund_params_include_giftcard_name_from_source(self):
        source_tx, refund_tx = self._make_source_and_refund("001", self.brand_vvv, "vvvgiftcard")
        params = self.brand_vvv._buckaroo_get_refund_params(source_tx, refund_tx)
        self.assertEqual(params["giftcard_name"], "vvvgiftcard")
        self.assertEqual(params["original_transaction_key"], "SRC_KEY_001")
        self.assertEqual(params["refund_amount"], 50.0)

    def test_refund_raises_without_service_code(self):
        source_tx = self._create_buckaroo_tx(
            reference="SRC-GC-NOSC", amount=50.0, payment_method=self.brand_vvv
        )
        source_tx.provider_reference = "SRC_KEY_NOSC"
        source_tx.buckaroo_official_service_code = False
        refund_tx = self._create_buckaroo_tx(
            reference="REF-GC-NOSC", amount=-50.0, payment_method=self.brand_vvv
        )
        with self.assertRaises(ValidationError):
            self.brand_vvv._buckaroo_get_refund_params(source_tx, refund_tx)

    def test_ideal_refund_has_no_giftcard_name(self):
        source_tx = self._create_buckaroo_tx(reference="SRC-IDEAL-GC", amount=50.0)
        source_tx.provider_reference = "SRC_IDEAL_KEY"
        refund_tx = self._create_buckaroo_tx(reference="REF-IDEAL-GC", amount=-50.0)
        params = self.ideal._buckaroo_get_refund_params(source_tx, refund_tx)
        self.assertNotIn("giftcard_name", params)

    def test_intersolve_refund_params_include_lastname_and_email(self):
        cases = [
            (self.brand_vvv, "vvvgiftcard"),
            (self.brand_boekenbon, "boekenbon"),
            (self.brand_webshop, "webshopgiftcard"),
            (self.brand_yourgift, "yourgift"),
        ]
        for brand, code in cases:
            with self.subTest(brand=code):
                source_tx, refund_tx = self._make_source_and_refund(
                    f"INT-{code}",
                    brand,
                    code,
                )
                params = brand._buckaroo_get_refund_params(source_tx, refund_tx)
                sp = params.get("service_parameters", {})
                self.assertEqual(sp.get("LastName"), "Buyer")
                self.assertEqual(sp.get("Email"), "norbert.buyer@example.com")

    def test_non_intersolve_refund_params_exclude_lastname_and_email(self):
        for brand, code in [(self.brand_fashioncheque, "fashioncheque")]:
            with self.subTest(brand=code):
                source_tx, refund_tx = self._make_source_and_refund(
                    f"NONINT-{code}",
                    brand,
                    code,
                )
                params = brand._buckaroo_get_refund_params(source_tx, refund_tx)
                sp = params.get("service_parameters", {})
                self.assertNotIn("LastName", sp)
                self.assertNotIn("Email", sp)

    def test_intersolve_refund_lastname_falls_back_when_partner_name_blank(self):
        source_tx, refund_tx = self._make_source_and_refund(
            "INT-BLANKNAME",
            self.brand_vvv,
            "vvvgiftcard",
        )
        source_tx.partner_id.name = ""
        params = self.brand_vvv._buckaroo_get_refund_params(source_tx, refund_tx)
        sp = params.get("service_parameters", {})
        self.assertEqual(sp.get("LastName"), "Customer")

    def test_intersolve_refund_omits_email_when_partner_email_blank(self):
        source_tx, refund_tx = self._make_source_and_refund(
            "INT-NOEMAIL",
            self.brand_vvv,
            "vvvgiftcard",
        )
        source_tx.partner_id.email = False
        params = self.brand_vvv._buckaroo_get_refund_params(source_tx, refund_tx)
        sp = params.get("service_parameters", {})
        self.assertIn("LastName", sp)
        self.assertNotIn("Email", sp)

    def test_redirect_mode_refund_resolves_brand_via_service_code(self):
        """Redirect-mode source_tx records the giftcard *parent* as
        payment_method_id (no backend field). Refund must still inject
        Intersolve LastName/Email by resolving the brand sub-method from
        ``buckaroo_official_service_code``."""
        source_tx, refund_tx = self._make_source_and_refund(
            "REDIR-VVV",
            self.giftcard,
            "vvvgiftcard",
        )
        params = self.giftcard._buckaroo_get_refund_params(source_tx, refund_tx)
        sp = params.get("service_parameters", {})
        self.assertEqual(params["giftcard_name"], "vvvgiftcard")
        self.assertEqual(sp.get("LastName"), "Buyer")
        self.assertEqual(sp.get("Email"), "norbert.buyer@example.com")

    def test_redirect_mode_refund_skips_intersolve_params_for_non_intersolve_brand(self):
        source_tx, refund_tx = self._make_source_and_refund(
            "REDIR-FC",
            self.giftcard,
            "fashioncheque",
        )
        params = self.giftcard._buckaroo_get_refund_params(source_tx, refund_tx)
        sp = params.get("service_parameters", {})
        self.assertNotIn("LastName", sp)
        self.assertNotIn("Email", sp)

    def test_intersolve_refund_uses_commercial_partner_for_lastname_and_email(self):
        """H6: when ``partner_id`` is a child contact, prefer the commercial
        partner (company / billing root) for the cardholder identity. Mirrors
        the convention used by other payment integrations."""
        company = self.env["res.partner"].create(
            {
                "name": "Acme NV",
                "email": "billing@acme.example",
                "is_company": True,
            }
        )
        contact = self.env["res.partner"].create(
            {
                "name": "Jane Contact",
                "email": "jane@acme.example",
                "parent_id": company.id,
            }
        )
        source_tx, refund_tx = self._make_source_and_refund(
            "INT-COMM",
            self.brand_vvv,
            "vvvgiftcard",
        )
        source_tx.partner_id = contact.id
        refund_tx.partner_id = contact.id
        params = self.brand_vvv._buckaroo_get_refund_params(source_tx, refund_tx)
        sp = params.get("service_parameters", {})
        self.assertEqual(sp.get("LastName"), "NV")
        self.assertEqual(sp.get("Email"), "billing@acme.example")


@tagged("post_install", "-at_install")
class TestGiftcardSkipFrameworkAmountValidation(_GiftcardTestBase):
    """Giftcards (parent or brand) never trigger framework's amount-mismatch error."""

    def test_extract_amount_data_returns_none_for_brand_tx(self):
        tx = self._create_buckaroo_tx(
            reference="TX-GC-AMT-001", amount=10.80, payment_method=self.brand_vvv
        )
        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-AMT-001",
                "brq_amount": "10.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
            }
        )
        self.assertIsNone(tx._extract_amount_data(parsed))

    def test_extract_amount_data_returns_none_for_parent_giftcard_tx(self):
        tx = self._create_buckaroo_tx(
            reference="TX-GC-AMT-002", amount=10.80, payment_method=self.giftcard
        )
        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-AMT-002",
                "brq_amount": "10.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
            }
        )
        self.assertIsNone(tx._extract_amount_data(parsed))

    def test_extract_amount_data_still_validates_non_giftcard_tx(self):
        tx = self._create_buckaroo_tx(reference="TX-IDEAL-AMT-001", amount=50.0)
        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-IDEAL-AMT-001",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
            }
        )
        result = tx._extract_amount_data(parsed)
        self.assertEqual(result, {"amount": 50.0, "currency_code": "EUR"})


@tagged("post_install", "-at_install")
class TestGiftcardPartialPayGuard(_GiftcardTestBase):
    """First push wins on a giftcard tx (parent or brand)."""

    def test_pending_push_does_not_overwrite_brand_provider_reference(self):
        tx = self._create_buckaroo_tx(reference="TX-GC-PP-001", payment_method=self.brand_vvv)
        tx.provider_reference = "ORIGINAL_GC_KEY"

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-PP-001",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "790",
                "brq_transactions": "REMAINDER_KEY",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.provider_reference, "ORIGINAL_GC_KEY")

    def test_success_push_does_not_overwrite_brand_provider_reference(self):
        tx = self._create_buckaroo_tx(reference="TX-GC-PP-002", payment_method=self.brand_vvv)
        tx.provider_reference = "ORIGINAL_GC_KEY_2"

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-PP-002",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "IDEAL_REMAINDER_KEY",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.provider_reference, "ORIGINAL_GC_KEY_2")

    def test_first_push_writes_provider_reference_when_empty(self):
        tx = self._create_buckaroo_tx(reference="TX-GC-PP-003", payment_method=self.brand_vvv)
        tx.provider_reference = False

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-PP-003",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "FIRST_GC_KEY",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.provider_reference, "FIRST_GC_KEY")

    def test_non_giftcard_pending_push_overwrites_provider_reference(self):
        tx = self._create_buckaroo_tx(reference="TX-IDEAL-PP-001")
        tx.provider_reference = "OLD_IDEAL_KEY"

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-IDEAL-PP-001",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "790",
                "brq_transactions": "NEW_IDEAL_KEY",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.provider_reference, "NEW_IDEAL_KEY")

    def test_giftcard_pending_push_sets_amount_when_partial(self):
        """Bounce-back left tx pending without a consumed amount; the push
        carries the actual drawn slice in ``brq_amount`` (e.g. 5.00 vs the
        original 10.80). ``_apply_updates`` must write that amount onto the
        tx before transitioning to done so ``sale.order.amount_paid``
        reflects reality.
        """
        tx = self._create_buckaroo_tx(
            reference="TX-GC-PEND-001",
            amount=10.80,
            payment_method=self.brand_vvv,
        )
        tx.provider_reference = "GC_PARTIAL_KEY"
        tx._set_pending()

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-PEND-001",
                "brq_amount": "5.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "GC_PARTIAL_KEY",
                "brq_transaction_method": "vvvgiftcard",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.amount, 5.00)
        self.assertEqual(tx.state, "done")

    def test_giftcard_pending_push_ignores_amount_equal_to_tx_amount(self):
        """If the push amount equals tx.amount, it's the requested amount
        not the consumed slice — don't overwrite tx.amount."""
        tx = self._create_buckaroo_tx(
            reference="TX-GC-PEND-002",
            amount=10.80,
            payment_method=self.brand_vvv,
        )
        tx.provider_reference = "GC_PARTIAL_KEY_2"
        tx._set_pending()

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-PEND-002",
                "brq_amount": "10.80",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "GC_PARTIAL_KEY_2",
                "brq_transaction_method": "vvvgiftcard",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.amount, 10.80)
        self.assertEqual(tx.state, "done")

    def test_non_giftcard_pending_push_does_not_change_amount(self):
        """Amount-overwrite is scoped to giftcards. iDEAL pending->done push
        must not mutate the tx amount (framework already validates it)."""
        tx = self._create_buckaroo_tx(reference="TX-IDEAL-AMT-PP-001", amount=50.0)
        tx.provider_reference = "IDEAL_KEY"
        tx._set_pending()

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-IDEAL-AMT-PP-001",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "IDEAL_KEY",
                "brq_transaction_method": "ideal",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.amount, 50.0)
        self.assertEqual(tx.state, "done")


@tagged("post_install", "-at_install")
class TestGiftcardServiceCodeGuard(_GiftcardTestBase):
    """First push wins on the giftcard tx's ``service_code`` too — a
    remainder push (e.g. ``ideal``) must not overwrite the giftcard brand
    recorded by the initial push, or refunds end up routed to the wrong
    gateway service.
    """

    def test_remainder_push_does_not_overwrite_brand_service_code(self):
        tx = self._create_buckaroo_tx(reference="TX-GC-SC-001", payment_method=self.brand_vvv)
        tx.buckaroo_official_service_code = "vvvgiftcard"

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-SC-001",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "REMAINDER_KEY",
                "brq_transaction_method": "ideal",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.buckaroo_official_service_code, "vvvgiftcard")

    def test_remainder_push_does_not_overwrite_parent_service_code(self):
        tx = self._create_buckaroo_tx(reference="TX-GC-SC-002", payment_method=self.giftcard)
        tx.buckaroo_official_service_code = "vvvgiftcard"

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-SC-002",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "790",
                "brq_transactions": "REMAINDER_KEY",
                "brq_transaction_method": "ideal",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.buckaroo_official_service_code, "vvvgiftcard")

    def test_first_push_writes_service_code_when_empty(self):
        tx = self._create_buckaroo_tx(reference="TX-GC-SC-003", payment_method=self.giftcard)
        tx.buckaroo_official_service_code = False

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-SC-003",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "FIRST_KEY",
                "brq_transaction_method": "vvvgiftcard",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.buckaroo_official_service_code, "vvvgiftcard")

    def test_non_giftcard_push_overwrites_service_code(self):
        tx = self._create_buckaroo_tx(reference="TX-IDEAL-SC-001")
        tx.buckaroo_official_service_code = "ideal"

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-IDEAL-SC-001",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "NEW_KEY",
                "brq_transaction_method": "ideal_other",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.buckaroo_official_service_code, "ideal_other")


@tagged("post_install", "-at_install")
class TestGiftcardNonGiftcardUnaffected(_GiftcardTestBase):
    """Regression: iDEAL payment creation untouched by giftcard override."""

    def test_ideal_create_payment_has_no_giftcard_params(self):
        tx = self._create_buckaroo_tx(reference="TX-IDEAL-REG-001")
        client = MagicMock()
        mock_builder, mock_response = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            result = self.ideal._buckaroo_create_payment(tx, client)

        params = MockPS.return_value.create_payment.call_args[0][1]
        self.assertNotIn("giftcard_name", params)
        self.assertNotIn("services_selectable_by_client", params)
        self.assertNotIn("continue_on_incomplete", params)
        mock_builder.pay.assert_called_once()
        self.assertEqual(result, mock_response)

    def test_ideal_create_payment_does_not_read_giftcard_context_keys(self):
        tx = self._create_buckaroo_tx(reference="TX-IDEAL-REG-002").with_context(
            buckaroo_giftcard_cardnumber="SHOULD-NOT-READ",
            buckaroo_giftcard_pin="9999",
        )
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()

        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.ideal._buckaroo_create_payment(tx, client)
        # The base method must not touch ``builder.add_parameter`` for the
        # giftcard cardnumber / PIN keys — only the giftcard branch does.
        added_calls = {call[0][0] for call in mock_builder.add_parameter.call_args_list}
        self.assertNotIn("IntersolveCardnumber", added_calls)
        self.assertNotIn("IntersolvePIN", added_calls)


@tagged("post_install", "-at_install")
class TestGiftcardControllerContextBridge(_GiftcardTestBase):
    """Controller bridges giftcard inline params from kwargs onto the
    request context (``request.update_context``) and pops them before
    handing off to super. The legacy ``buckaroo_giftcard_brand`` key is
    discarded silently for forward-compat with old browser-cached forms."""

    def _call_controller(self, **kwargs):
        import odoo.http
        from ..controllers.giftcard import GiftcardPaymentPortal

        portal = GiftcardPaymentPortal()
        fake_req = MagicMock()
        fake_req.env = self.env
        with (
            patch(
                "odoo.addons.website_sale.controllers.payment.PaymentPortal.shop_payment_transaction",
                return_value="SUPER_OK",
            ) as super_stub,
            patch.object(odoo.http, "request", fake_req),
            patch(
                "odoo.addons.payment_buckaroo_official.controllers.giftcard.request",
                new=fake_req,
            ),
        ):
            result = portal.shop_payment_transaction(
                order_id=1,
                access_token="tok",
                **kwargs,
            )
            return result, super_stub, fake_req

    def test_giftcard_kwargs_are_pushed_to_request_context(self):
        _, _, fake_req = self._call_controller(
            payment_method_id=self.giftcard.id,
            buckaroo_giftcard_cardnumber="VVV-CARD-XYZ",
            buckaroo_giftcard_pin="1234",
        )
        fake_req.update_context.assert_called_once_with(
            buckaroo_giftcard_cardnumber="VVV-CARD-XYZ",
            buckaroo_giftcard_pin="1234",
        )

    def test_giftcard_kwargs_are_popped_from_super_call(self):
        _, super_stub, _ = self._call_controller(
            payment_method_id=self.giftcard.id,
            buckaroo_giftcard_cardnumber="VVV-CARD-XYZ",
            buckaroo_giftcard_pin="1234",
        )
        _, kwargs_to_super = super_stub.call_args
        self.assertNotIn("buckaroo_giftcard_cardnumber", kwargs_to_super)
        self.assertNotIn("buckaroo_giftcard_pin", kwargs_to_super)

    def test_legacy_brand_kwarg_is_dropped_silently(self):
        """Defensive: stale ``buckaroo_giftcard_brand`` from a cached form
        must not be forwarded to ``super`` or pushed into the context."""
        _, super_stub, fake_req = self._call_controller(
            payment_method_id=self.giftcard.id,
            buckaroo_giftcard_brand="vvvgiftcard",
            buckaroo_giftcard_cardnumber="VVV-CARD-XYZ",
            buckaroo_giftcard_pin="1234",
        )
        _, kwargs_to_super = super_stub.call_args
        self.assertNotIn("buckaroo_giftcard_brand", kwargs_to_super)
        passed = fake_req.update_context.call_args.kwargs
        self.assertNotIn("buckaroo_giftcard_brand", passed)

    def test_no_giftcard_kwargs_skips_context_push(self):
        result, _, fake_req = self._call_controller()
        self.assertEqual(result, "SUPER_OK")
        fake_req.update_context.assert_not_called()

    def test_super_result_is_returned(self):
        result, _, _ = self._call_controller(
            payment_method_id=self.giftcard.id,
            buckaroo_giftcard_cardnumber="VVV-CARD-XYZ",
            buckaroo_giftcard_pin="1234",
        )
        self.assertEqual(result, "SUPER_OK")


@tagged("post_install", "-at_install")
class TestGiftcardCheckoutVisibility(_GiftcardTestBase):
    """``_get_compatible_payment_methods`` surfaces giftcard parent vs.
    brand sub-methods based on the parent's mode."""

    def _compatible(self):
        return self.env["payment.method"]._get_compatible_payment_methods(
            provider_ids=[self.buckaroo.id],
            partner_id=self.partner.id,
            currency_id=self.currency_euro.id,
        )

    def test_redirect_mode_hides_brand_sub_methods_from_checkout(self):
        self.giftcard.buckaroo_official_giftcard_method = "redirect"
        methods = self._compatible()
        self.assertIn(self.giftcard, methods)
        for brand in (
            self.brand_vvv,
            self.brand_fashioncheque,
            self.brand_boekenbon,
            self.brand_webshop,
            self.brand_yourgift,
        ):
            with self.subTest(brand=brand.code):
                self.assertNotIn(brand, methods)

    def test_inline_mode_hides_parent_shows_brand_sub_methods(self):
        self.giftcard.buckaroo_official_giftcard_method = "inline"
        methods = self._compatible()
        self.assertNotIn(self.giftcard, methods)
        for brand in (
            self.brand_vvv,
            self.brand_fashioncheque,
            self.brand_boekenbon,
            self.brand_webshop,
            self.brand_yourgift,
        ):
            with self.subTest(brand=brand.code):
                self.assertIn(brand, methods)

    def test_non_buckaroo_provider_unaffected(self):
        self.giftcard.buckaroo_official_giftcard_method = "inline"
        methods = self.env["payment.method"]._get_compatible_payment_methods(
            provider_ids=[self.dummy_provider.id],
            partner_id=self.partner.id,
            currency_id=self.currency_euro.id,
        )
        # Dummy provider's only method is pm_unknown (primary); no giftcard
        # brand should leak in just because the buckaroo parent is inline.
        self.assertIn(self.pm_unknown, methods)
        for brand in (
            self.brand_vvv,
            self.brand_fashioncheque,
            self.brand_boekenbon,
            self.brand_webshop,
            self.brand_yourgift,
        ):
            with self.subTest(brand=brand.code):
                self.assertNotIn(brand, methods)
        self.assertNotIn(self.giftcard, methods)

    def test_brand_method_amount_limits_apply(self):
        """Brand sub-methods surfaced in inline mode must still respect
        Buckaroo's amount-limit filter from the base override."""
        self.giftcard.buckaroo_official_giftcard_method = "inline"
        self.brand_vvv.buckaroo_official_min_amount = "100.00"
        methods = self.env["payment.method"]._get_compatible_payment_methods(
            provider_ids=[self.buckaroo.id],
            partner_id=self.partner.id,
            currency_id=self.currency_euro.id,
            amount=50.0,
        )
        self.assertNotIn(self.brand_vvv, methods)
        # Other brands without a min still surface.
        self.assertIn(self.brand_boekenbon, methods)

    def test_inactive_brand_does_not_surface_in_inline_mode(self):
        """H3: super skipped brand sub-methods, so the inline-mode surfacer
        must re-apply the ``active`` filter — otherwise deactivated brands
        leak into checkout."""
        self.giftcard.buckaroo_official_giftcard_method = "inline"
        self.brand_vvv.active = False
        try:
            methods = self._compatible()
        finally:
            self.brand_vvv.active = True
        self.assertNotIn(self.brand_vvv, methods)
        # Sibling active brands still surface.
        self.assertIn(self.brand_boekenbon, methods)


@tagged("post_install", "-at_install")
class TestGiftcardRemainderCancelDemotesDone(_GiftcardTestBase):
    """Giftcard partial-pay: success push (190, key=GC) marks tx done; a
    later remainder cancel push (890, key=IDEAL) must demote the tx to
    ``cancel`` so the order doesn't stay paid when the shopper bailed
    on the remainder.
    """

    def test_giftcard_remainder_cancel_push_demotes_done_to_cancel(self):
        tx = self._create_buckaroo_tx(reference="TX-GC-CANCEL-001", payment_method=self.brand_vvv)
        tx.provider_reference = "GC_SUCCESS_KEY"
        tx.buckaroo_official_service_code = "vvvgiftcard"
        tx._set_done()

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-CANCEL-001",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "890",
                "brq_transactions": "IDEAL_REMAINDER_KEY",
                "brq_transaction_method": "ideal",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.state, "cancel")

    def test_giftcard_remainder_failed_push_demotes_done_to_cancel(self):
        tx = self._create_buckaroo_tx(reference="TX-GC-CANCEL-002", payment_method=self.brand_vvv)
        tx.provider_reference = "GC_SUCCESS_KEY_2"
        tx.buckaroo_official_service_code = "vvvgiftcard"
        tx._set_done()

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-CANCEL-002",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "490",
                "brq_transactions": "IDEAL_REMAINDER_KEY",
                "brq_transaction_method": "ideal",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.state, "cancel")

    def test_giftcard_remainder_success_push_keeps_done(self):
        tx = self._create_buckaroo_tx(reference="TX-GC-CANCEL-003", payment_method=self.brand_vvv)
        tx.provider_reference = "GC_SUCCESS_KEY_3"
        tx.buckaroo_official_service_code = "vvvgiftcard"
        tx._set_done()

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-CANCEL-003",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "IDEAL_REMAINDER_KEY",
                "brq_transaction_method": "ideal",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.state, "done")

    def test_giftcard_duplicate_push_with_same_key_still_skipped(self):
        tx = self._create_buckaroo_tx(reference="TX-GC-CANCEL-004", payment_method=self.brand_vvv)
        tx.provider_reference = "GC_SUCCESS_KEY_4"
        tx.buckaroo_official_service_code = "vvvgiftcard"
        tx._set_done()

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-CANCEL-004",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "890",
                "brq_transactions": "GC_SUCCESS_KEY_4",
                "brq_transaction_method": "vvvgiftcard",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.state, "done")


@tagged("post_install", "-at_install")
class TestGiftcardRefundRoutingAllBrands(_GiftcardTestBase):
    """All 7 giftcard brand refunds route through the ``giftcards`` SDK
    builder (not the brand-code DefaultBuilder), with ``giftcard_name`` set
    to the original brand. iDEAL refunds must NOT take this path.
    """

    def _make_pair(self, suffix, brand_method, service_code):
        source_tx = self._create_buckaroo_tx(
            reference=f"SRC-GCREF-{suffix}",
            amount=50.0,
            payment_method=brand_method,
        )
        source_tx.provider_reference = f"SRC_KEY_{suffix}"
        source_tx.buckaroo_official_service_code = service_code
        refund_tx = self._create_buckaroo_tx(
            reference=f"REF-GCREF-{suffix}",
            amount=-50.0,
            payment_method=brand_method,
        )
        return source_tx, refund_tx

    def _all_brands(self):
        return (
            (self.brand_vvv, "vvvgiftcard"),
            (self.brand_fashioncheque, "fashioncheque"),
            (self.brand_boekenbon, "boekenbon"),
            (self.brand_webshop, "webshopgiftcard"),
            (self.brand_yourgift, "yourgift"),
        )

    def test_each_brand_refund_invokes_giftcards_service_builder(self):
        client = MagicMock()
        for idx, (brand, code) in enumerate(self._all_brands()):
            with self.subTest(brand=code):
                suffix = f"{idx:02d}-{code}"
                source_tx, refund_tx = self._make_pair(suffix, brand, code)
                mock_builder, _ = make_mock_sdk_builder()
                with patch(
                    "odoo.addons.payment_buckaroo_official.models."
                    "payment_method_giftcard.PaymentService"
                ) as MockPS:
                    MockPS.return_value.create_payment.return_value = mock_builder
                    brand._buckaroo_create_refund(source_tx, refund_tx, client)
                    MockPS.return_value.create_payment.assert_called_once()
                    service_arg, params = MockPS.return_value.create_payment.call_args[0]
                self.assertEqual(service_arg, "giftcards")
                self.assertEqual(params.get("giftcard_name"), code)
                self.assertEqual(params.get("original_transaction_key"), f"SRC_KEY_{suffix}")
                self.assertEqual(params.get("refund_amount"), 50.0)
                mock_builder.refund.assert_called_once()

    def test_boekenbon_refund_regression_tx_129087(self):
        """Regression for tx 129087: boekenbon (Intersolve backend) refund
        previously routed via brand code -> DefaultBuilder -> 491 Validation
        failure. Must now route through the ``giftcards`` service builder.
        """
        source_tx, refund_tx = self._make_pair("REG-129087", self.brand_boekenbon, "boekenbon")
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()
        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_giftcard.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.brand_boekenbon._buckaroo_create_refund(source_tx, refund_tx, client)
        service_arg, params = MockPS.return_value.create_payment.call_args[0]
        self.assertEqual(service_arg, "giftcards")
        self.assertEqual(params.get("giftcard_name"), "boekenbon")
        mock_builder.refund.assert_called_once()

    def test_ideal_refund_does_not_route_through_giftcards_builder(self):
        source_tx = self._create_buckaroo_tx(reference="SRC-IDEAL-RT", amount=50.0)
        source_tx.provider_reference = "SRC_IDEAL_RT_KEY"
        refund_tx = self._create_buckaroo_tx(reference="REF-IDEAL-RT", amount=-50.0)
        client = MagicMock()
        mock_builder, _ = make_mock_sdk_builder()
        with (
            patch(
                "odoo.addons.payment_buckaroo_official.models."
                "payment_method_giftcard.PaymentService"
            ) as MockGiftPS,
            patch(
                "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
            ) as MockBasePS,
        ):
            MockBasePS.return_value.create_payment.return_value = mock_builder
            self.ideal._buckaroo_create_refund(source_tx, refund_tx, client)
            MockGiftPS.return_value.create_payment.assert_not_called()
            MockBasePS.return_value.create_payment.assert_called_once()
            service_arg, params = MockBasePS.return_value.create_payment.call_args[0]
        self.assertEqual(service_arg, "ideal")
        self.assertNotIn("giftcard_name", params)
        mock_builder.refund.assert_called_once()

    def test_non_giftcard_done_tx_skips_any_push(self):
        tx = self._create_buckaroo_tx(reference="TX-IDEAL-DONE-001")
        tx.provider_reference = "IDEAL_KEY"
        tx._set_done()

        parsed = parsed_from_form(
            {
                "brq_invoicenumber": "TX-IDEAL-DONE-001",
                "brq_amount": "50.00",
                "brq_currency": "EUR",
                "brq_statuscode": "890",
                "brq_transactions": "OTHER_KEY",
                "brq_transaction_method": "ideal",
            }
        )
        tx._apply_updates(parsed)
        self.assertEqual(tx.state, "done")


@tagged("post_install", "-at_install")
class TestGiftcardRedirectModeRemainderPush(_GiftcardTestBase):
    """Redirect-mode partial: Buckaroo's hosted picker drives the remainder
    on the gateway, then posts a separate iDEAL push back to Odoo carrying
    ``brq_relatedtransaction_partialpayment`` linking it to the giftcard
    slice. The framework's ``_search_by_reference`` matches the SO ref and
    lands on the already-done giftcard tx, so the success push is dropped on
    the floor (``_apply_updates`` returns when ``state in ('done',...)`` and
    the cancel/failed demote-branch doesn't apply). The push must split off
    into a sibling ``payment.transaction`` for the remainder method so
    ``sale.order.amount_paid`` matches ``amount_total`` and the order can
    confirm. Regression for order S11674 (id 11691).
    """

    def _giftcard_done_tx(self, reference, amount=25.00):
        tx = self._create_buckaroo_tx(
            reference=reference,
            amount=amount,
            payment_method=self.brand_boekenbon,
        )
        tx.provider_reference = "GC_DONE_KEY"
        tx.buckaroo_official_service_code = "boekenbon"
        tx._set_done()
        return tx

    def _remainder_push(
        self, reference, amount=38.00, method="ideal", key="IDEAL_REM_KEY", related="GC_DONE_KEY"
    ):
        return parsed_from_form(
            {
                "brq_invoicenumber": reference,
                "brq_amount": f"{amount:.2f}",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": key,
                "brq_transaction_method": method,
                "brq_relatedtransaction_partialpayment": related,
            }
        )

    def test_remainder_push_creates_sibling_tx(self):
        gc_tx = self._giftcard_done_tx("TX-GC-RP-RPSH-001")
        push = self._remainder_push("TX-GC-RP-RPSH-001")
        sibling = gc_tx._buckaroo_split_remainder_push(push)

        self.assertTrue(sibling)
        self.assertNotEqual(sibling.id, gc_tx.id)
        self.assertEqual(sibling.payment_method_id, self.ideal)
        self.assertEqual(sibling.amount, 38.00)
        # Independent leg, not a child of the giftcard tx — so Odoo does not
        # treat it as a refund of the giftcard payment (see _buckaroo_split_
        # remainder_push). Matches inline mode.
        self.assertFalse(sibling.source_transaction_id)
        self.assertEqual(sibling.provider_id, gc_tx.provider_id)
        self.assertEqual(sibling.partner_id, gc_tx.partner_id)
        self.assertEqual(sibling.currency_id, gc_tx.currency_id)
        self.assertEqual(sibling.state, "draft")

    def test_remainder_push_processes_to_done(self):
        gc_tx = self._giftcard_done_tx("TX-GC-RP-RPSH-002")
        push = self._remainder_push("TX-GC-RP-RPSH-002")
        sibling = gc_tx._buckaroo_split_remainder_push(push)
        sibling._process("buckaroo_official", push)

        self.assertEqual(sibling.state, "done")
        self.assertEqual(sibling.provider_reference, "IDEAL_REM_KEY")
        self.assertEqual(sibling.buckaroo_official_service_code, "ideal")

    def test_remainder_split_leaves_giftcard_tx_unchanged(self):
        gc_tx = self._giftcard_done_tx("TX-GC-RP-RPSH-003")
        push = self._remainder_push("TX-GC-RP-RPSH-003")
        gc_tx._buckaroo_split_remainder_push(push)

        self.assertEqual(gc_tx.state, "done")
        self.assertEqual(gc_tx.amount, 25.00)
        self.assertEqual(gc_tx.provider_reference, "GC_DONE_KEY")
        self.assertEqual(gc_tx.buckaroo_official_service_code, "boekenbon")

    def test_remainder_push_idempotent_on_redelivery(self):
        """Buckaroo may resend the push; a sibling already keyed by the
        push's ``transaction_key`` must not be duplicated."""
        gc_tx = self._giftcard_done_tx("TX-GC-RP-RPSH-004")
        push = self._remainder_push("TX-GC-RP-RPSH-004")
        first = gc_tx._buckaroo_split_remainder_push(push)
        first._process("buckaroo_official", push)

        second = gc_tx._buckaroo_split_remainder_push(push)
        self.assertFalse(second)

    def test_non_remainder_push_returns_false(self):
        """A push without ``brq_relatedtransaction_partialpayment`` must
        not be treated as a remainder, even if the giftcard tx is done."""
        gc_tx = self._giftcard_done_tx("TX-GC-RP-RPSH-005")
        push = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-RP-RPSH-005",
                "brq_amount": "25.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "GC_DONE_KEY",
                "brq_transaction_method": "boekenbon",
            }
        )
        self.assertFalse(gc_tx._buckaroo_split_remainder_push(push))

    def test_non_giftcard_done_tx_does_not_split(self):
        """Only giftcard txs split off remainders. A stray push with
        ``relatedtransaction_partialpayment`` arriving on a plain iDEAL
        tx must NOT spawn a sibling."""
        tx = self._create_buckaroo_tx(reference="TX-IDEAL-RPSH-001")
        tx.provider_reference = "IDEAL_DONE_KEY"
        tx.buckaroo_official_service_code = "ideal"
        tx._set_done()
        push = parsed_from_form(
            {
                "brq_invoicenumber": "TX-IDEAL-RPSH-001",
                "brq_amount": "10.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "OTHER_KEY",
                "brq_transaction_method": "boekenbon",
                "brq_relatedtransaction_partialpayment": "IDEAL_DONE_KEY",
            }
        )
        self.assertFalse(tx._buckaroo_split_remainder_push(push))

    def test_remainder_push_unknown_service_method_returns_false(self):
        """If the push's service_code doesn't map to any payment.method on
        the provider, skip the split rather than blowing up."""
        gc_tx = self._giftcard_done_tx("TX-GC-RP-RPSH-007")
        push = self._remainder_push(
            "TX-GC-RP-RPSH-007",
            method="not_a_real_buckaroo_service",
        )
        self.assertFalse(gc_tx._buckaroo_split_remainder_push(push))

    def test_remainder_push_failed_status_returns_false(self):
        """Failed/pending remainder pushes go through the existing demote
        logic, not the split. Only success (190) creates a sibling."""
        gc_tx = self._giftcard_done_tx("TX-GC-RP-RPSH-008")
        push = parsed_from_form(
            {
                "brq_invoicenumber": "TX-GC-RP-RPSH-008",
                "brq_amount": "38.00",
                "brq_currency": "EUR",
                "brq_statuscode": "890",
                "brq_transactions": "IDEAL_FAIL_KEY",
                "brq_transaction_method": "ideal",
                "brq_relatedtransaction_partialpayment": "GC_DONE_KEY",
            }
        )
        self.assertFalse(gc_tx._buckaroo_split_remainder_push(push))

    def test_remainder_push_splits_when_giftcard_tx_still_draft(self):
        """C2: out-of-order race. If iDEAL remainder push arrives BEFORE the
        giftcard slice's own push, the source giftcard tx is still draft.
        Split must still fire so ``_apply_updates`` doesn't stamp the
        giftcard tx with iDEAL's identity (key, service_code, amount)."""
        gc_tx = self._create_buckaroo_tx(
            reference="TX-GC-RP-RACE-001",
            amount=25.00,
            payment_method=self.brand_boekenbon,
        )
        gc_tx.buckaroo_official_service_code = "boekenbon"
        push = self._remainder_push("TX-GC-RP-RACE-001")
        sibling = gc_tx._buckaroo_split_remainder_push(push)

        self.assertTrue(sibling)
        self.assertEqual(sibling.payment_method_id, self.ideal)
        self.assertEqual(sibling.amount, 38.00)
        # Source tx untouched — still draft, still owns the giftcard identity.
        self.assertEqual(gc_tx.state, "draft")
        self.assertEqual(gc_tx.buckaroo_official_service_code, "boekenbon")

    def test_remainder_push_splits_on_json_partial_relation(self):
        """C3: JSON pushes carry ``RelatedTransactions[]`` with
        ``RelationType=PartialPayment`` instead of the form key. The
        detector must handle both wire shapes."""
        gc_tx = self._giftcard_done_tx("TX-GC-RP-JSON-001")
        push = parsed_from_json(
            {
                "Transaction": {
                    "Key": "IDEAL_JSON_KEY",
                    "Invoice": "TX-GC-RP-JSON-001",
                    "AmountDebit": 38.00,
                    "Currency": "EUR",
                    "Status": {"Code": {"Code": 190}},
                    "Services": [{"Name": "ideal"}],
                    "RelatedTransactions": [
                        {
                            "RelationType": "PartialPayment",
                            "RelatedTransactionKey": "GC_DONE_KEY",
                        },
                    ],
                },
            }
        )
        sibling = gc_tx._buckaroo_split_remainder_push(push)

        self.assertTrue(sibling)
        self.assertEqual(sibling.payment_method_id, self.ideal)
        self.assertEqual(sibling.amount, 38.00)

    def test_remainder_push_ignores_json_unrelated_relation(self):
        """JSON pushes may carry other ``RelationType`` values (e.g. refund,
        capture). Only ``PartialPayment`` triggers split."""
        gc_tx = self._giftcard_done_tx("TX-GC-RP-JSON-002")
        push = parsed_from_json(
            {
                "Transaction": {
                    "Key": "IDEAL_JSON_KEY_2",
                    "Invoice": "TX-GC-RP-JSON-002",
                    "AmountDebit": 38.00,
                    "Currency": "EUR",
                    "Status": {"Code": {"Code": 190}},
                    "Services": [{"Name": "ideal"}],
                    "RelatedTransactions": [
                        {
                            "RelationType": "Refund",
                            "RelatedTransactionKey": "GC_DONE_KEY",
                        },
                    ],
                },
            }
        )
        self.assertFalse(gc_tx._buckaroo_split_remainder_push(push))

    def test_sibling_tx_inherits_sale_order_links(self):
        """M2: the remainder sibling tx must be linked to the same sale
        orders as the source giftcard tx so SO confirmation and
        ``amount_paid`` accounting hold."""
        gc_tx = self._giftcard_done_tx("TX-GC-RP-SO-001")
        order = self.env["sale.order"].create({"partner_id": self.partner.id})
        gc_tx.sale_order_ids = [Command.set(order.ids)]
        push = self._remainder_push("TX-GC-RP-SO-001")
        sibling = gc_tx._buckaroo_split_remainder_push(push)

        self.assertEqual(sibling.sale_order_ids, order)


@tagged("post_install", "-at_install")
class TestGiftcardPartialRefund690Hint(_GiftcardTestBase):
    """Plaza rejects Intersolve giftcard refunds with status 690 when partial
    refunds are not enabled on the merchant account or LastName/Email service
    params are missing (per https://docs.buckaroo.io/docs/giftcards-integration
    #partial-refunds). The bare ``"failed with status code: 690"`` message
    leaves the admin guessing. Surface a hint naming the two requirements and
    (when present) include the SDK's gateway message verbatim.
    """

    def _refund_response_690(self, message=""):
        """Build a refund SDK response: status code 690 + optional gateway message."""
        response = MagicMock()
        response.key = "RFND_690_KEY"
        response.status_code = 200
        mock_sc = MagicMock()
        mock_sc.code = 690
        mock_st = MagicMock()
        mock_st.code = mock_sc
        response.status = mock_st
        response.is_successful.return_value = False
        response.is_pending.return_value = False
        response.is_cancelled.return_value = False
        response.is_failed.return_value = True
        response.get_some_error.return_value = message
        return response

    def _refund_tx(self, reference="R-TX-GC-690-001"):
        source_tx = self._create_buckaroo_tx(
            reference="TX-GC-690-SRC-001",
            amount=5.00,
            payment_method=self.brand_vvv,
        )
        source_tx.provider_reference = "GC_PARTIAL_KEY"
        source_tx.buckaroo_official_service_code = "vvvgiftcard"
        source_tx._set_done()
        refund_tx = self._create_buckaroo_tx(
            reference=reference,
            amount=-5.00,
            payment_method=self.brand_vvv,
        )
        refund_tx.source_transaction_id = source_tx.id
        return refund_tx

    def test_690_refund_state_message_names_intersolve_and_lastname_email(self):
        refund_tx = self._refund_tx()
        response = self._refund_response_690()
        refund_tx._buckaroo_official_handle_response_status(response, "refund")
        self.assertEqual(refund_tx.state, "error")
        self.assertIn("Plaza", refund_tx.state_message)
        self.assertIn("Intersolve", refund_tx.state_message)
        self.assertIn("LastName and Email", refund_tx.state_message)

    def test_690_refund_state_message_appends_gateway_message_when_present(self):
        refund_tx = self._refund_tx(reference="R-TX-GC-690-002")
        response = self._refund_response_690(message="Refund not allowed on partial.")
        refund_tx._buckaroo_official_handle_response_status(response, "refund")
        self.assertEqual(refund_tx.state, "error")
        self.assertIn("Refund not allowed on partial.", refund_tx.state_message)

    def test_690_non_refund_operation_keeps_generic_message(self):
        """The hint is scoped to ``refund``; a 690 on capture/void stays generic."""
        tx = self._create_buckaroo_tx(
            reference="TX-GC-690-CAP-001",
            amount=5.00,
            payment_method=self.brand_vvv,
        )
        response = self._refund_response_690()
        tx._buckaroo_official_handle_response_status(response, "capture")
        self.assertEqual(tx.state, "error")
        self.assertNotIn("LastName and Email", tx.state_message)
        self.assertIn("690", tx.state_message)

    def test_non_690_refund_keeps_generic_message(self):
        refund_tx = self._refund_tx(reference="R-TX-GC-690-003")
        response = self._refund_response_690()
        response.status.code.code = 491
        refund_tx._buckaroo_official_handle_response_status(response, "refund")
        self.assertEqual(refund_tx.state, "error")
        self.assertNotIn("LastName and Email", refund_tx.state_message)
        self.assertIn("491", refund_tx.state_message)

    def test_690_refund_non_intersolve_brand_keeps_generic_message(self):
        """H1: the Intersolve hint must NOT fire on a non-Intersolve refund
        (e.g. iDEAL, fashioncheque). Generic message only."""
        source_tx = self._create_buckaroo_tx(
            reference="TX-IDEAL-690-SRC-001",
            amount=5.00,
            payment_method=self.ideal,
        )
        source_tx.provider_reference = "IDEAL_KEY"
        source_tx.buckaroo_official_service_code = "ideal"
        source_tx._set_done()
        refund_tx = self._create_buckaroo_tx(
            reference="R-TX-IDEAL-690-001",
            amount=-5.00,
            payment_method=self.ideal,
        )
        refund_tx.source_transaction_id = source_tx.id
        response = self._refund_response_690()
        refund_tx._buckaroo_official_handle_response_status(response, "refund")
        self.assertEqual(refund_tx.state, "error")
        self.assertNotIn("Intersolve", refund_tx.state_message)
        self.assertNotIn("LastName and Email", refund_tx.state_message)
        self.assertIn("690", refund_tx.state_message)

    def test_690_refund_fashioncheque_keeps_generic_message(self):
        """H1: fashioncheque is a giftcard brand but NOT an Intersolve brand —
        the hint must not fire."""
        source_tx = self._create_buckaroo_tx(
            reference="TX-FC-690-SRC-001",
            amount=5.00,
            payment_method=self.brand_fashioncheque,
        )
        source_tx.provider_reference = "FC_KEY"
        source_tx.buckaroo_official_service_code = "fashioncheque"
        source_tx._set_done()
        refund_tx = self._create_buckaroo_tx(
            reference="R-TX-FC-690-001",
            amount=-5.00,
            payment_method=self.brand_fashioncheque,
        )
        refund_tx.source_transaction_id = source_tx.id
        response = self._refund_response_690()
        refund_tx._buckaroo_official_handle_response_status(response, "refund")
        self.assertEqual(refund_tx.state, "error")
        self.assertNotIn("Intersolve", refund_tx.state_message)
        self.assertNotIn("LastName and Email", refund_tx.state_message)
        self.assertIn("690", refund_tx.state_message)


@tagged("post_install", "-at_install")
class TestGiftcardRefundTargetResolution(_GiftcardTestBase):
    """A Plaza refund push whose invoice number matches the giftcard partial
    leg must spawn the R- child on the GCR child resolved by the
    ``RelatedTransactions.Refund`` key, not on the invoice-matched parent."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(PaymentTransaction, "_post_process", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_refund_resolves_gcr_child_not_invoice_match(self):
        parent = self._create_buckaroo_tx(
            reference="GC-RES-001", amount=15.0, payment_method=self.giftcard
        )
        parent.provider_reference = "PARENT_KEY"
        parent._set_done()
        child = self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": self.ideal.id,
                "reference": "GC-RES-001-GCR-XYZ",
                "amount": 7.5,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
            }
        )
        child.provider_reference = "GCR_CHILD_KEY"
        child._set_done()

        _route_giftcard_push(
            self.env,
            parsed_from_form(
                {
                    "brq_invoicenumber": "GC-RES-001",
                    "brq_amount_credit": "7.50",
                    "brq_currency": "EUR",
                    "brq_statuscode": "190",
                    "brq_transactions": "GCR_REFUND_KEY",
                    "brq_relatedtransaction_refund": "GCR_CHILD_KEY",
                    "brq_transaction_method": "ideal",
                }
            ),
        )

        refund = child.child_transaction_ids.filtered(lambda t: t.operation == "refund")
        self.assertEqual(len(refund), 1, "refund child should hang off the GCR child")
        self.assertEqual(refund.source_transaction_id, child)
        self.assertEqual(refund.amount, -7.5)
        self.assertEqual(refund.state, "done")
        self.assertFalse(
            parent.child_transaction_ids.filtered(lambda t: t.operation == "refund"),
            "parent partial leg must not gain a refund child",
        )


@tagged("post_install", "-at_install")
class TestGiftcardSameBrandMultiPartial(_GiftcardTestBase):
    """A remainder push for the SAME giftcard brand as the parent leg must
    still spawn its own child tx (3-leg chains must not drop the middle leg)."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(PaymentTransaction, "_post_process", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_same_brand_remainder_push_spawns_sibling_leg(self):
        vvv_service = self.brand_vvv.buckaroo_official_sdk_service_name
        parent = self._create_buckaroo_tx(
            reference="GC-SB-001", amount=10.0, payment_method=self.giftcard
        )
        parent.provider_reference = "LEG1_KEY"
        parent.buckaroo_official_service_code = vvv_service
        parent._set_done()

        _route_giftcard_push(
            self.env,
            parsed_from_form(
                {
                    "brq_invoicenumber": "GC-SB-001",
                    "brq_amount": "5.00",
                    "brq_currency": "EUR",
                    "brq_statuscode": "190",
                    "brq_transactions": "LEG2_KEY",
                    "brq_transaction_method": vvv_service,
                    "brq_relatedtransaction_partialpayment": "LEG1_KEY",
                }
            ),
        )

        # The 2nd leg spawns its own tx (3-leg chains must not drop the middle
        # leg) as an INDEPENDENT payment — not a child of the parent, so its
        # amount is never treated as a refund of the parent's payment.
        sibling = self.env["payment.transaction"].search(
            [("reference", "=", "GC-SB-001-GCR-LEG2_KEY")]
        )
        self.assertEqual(len(sibling), 1, "same-brand 2nd leg must spawn its own tx")
        self.assertEqual(sibling.provider_reference, "LEG2_KEY")
        self.assertEqual(sibling.amount, 5.0)
        self.assertFalse(sibling.source_transaction_id)
        self.assertFalse(parent.child_transaction_ids)


@tagged("post_install", "-at_install")
class TestGiftcardPbnkSuppression(_GiftcardTestBase):
    """Redirect mode: giftcard legs keep their PBNK just like inline mode —
    partial slices, full-cover payments, GCR children, same-brand remainder
    legs, and non-giftcard methods are all independently refundable from
    Odoo."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.giftcard.buckaroo_official_giftcard_method = "redirect"

    def test_partial_giftcard_leg_keeps_pbnk(self):
        tx = self._gc_tx("GC-PBNK-PARTIAL", self.order_total - 10.0)
        payment = tx._create_payment()
        self.assertTrue(payment)
        self.assertEqual(payment.amount, self.order_total - 10.0)

    def test_full_cover_giftcard_keeps_pbnk(self):
        tx = self._gc_tx("GC-PBNK-FULL", self.order_total)
        payment = tx._create_payment()
        self.assertTrue(payment)
        self.assertEqual(payment.amount, self.order_total)

    def test_gcr_child_keeps_pbnk(self):
        parent = self._gc_tx("GC-PBNK-PARENT", self.order_total - 10.0)
        child = self._gc_tx("GC-PBNK-PARENT-GCR-K", 10.0, payment_method=self.ideal, source=parent)
        payment = child._create_payment()
        self.assertTrue(payment)
        self.assertEqual(payment.amount, 10.0)

    def test_non_giftcard_keeps_pbnk(self):
        tx = self._gc_tx("GC-PBNK-IDEAL", self.order_total - 10.0, payment_method=self.ideal)
        payment = tx._create_payment()
        self.assertTrue(payment)
        self.assertEqual(payment.amount, self.order_total - 10.0)

    def test_same_brand_gcr_child_keeps_pbnk(self):
        """A redirect-spawned GCR sibling that happens to be a giftcard brand
        (same-brand remainder) must STILL get a PBNK — it is a genuine
        remainder leg, not a partial slice of the order total."""
        parent = self._gc_tx("GC-PBNK-SBPARENT", self.order_total - 10.0)
        child = self._gc_tx(
            "GC-PBNK-SBPARENT-GCR-K", 10.0, payment_method=self.brand_vvv, source=parent
        )
        payment = child._create_payment()
        self.assertTrue(payment)
        self.assertEqual(payment.amount, 10.0)

    def test_redirect_second_giftcard_brand_leg_keeps_pbnk(self):
        """Redirect mode: a second giftcard-brand partial leg still gets its
        own PBNK, same as any other leg. The controller leaves it an
        independent payment — no ``source_transaction_id`` linkage."""
        root = self._gc_tx("GC-PBNK-LINK-ROOT", 40.0)
        root.provider_reference = "LINK_ROOT_KEY"
        root.buckaroo_official_service_code = self.brand_vvv.buckaroo_official_sdk_service_name
        root._set_done()

        # The order remainder is 60; the upstream creates the second brand leg
        # at that remainder. Run it through the controller hook.
        leg = self._gc_tx("GC-PBNK-LINK-LEG", 60.0, payment_method=self.brand_vvv)
        GiftcardPaymentPortal()._validate_transaction_for_order(leg, self.order)

        self.assertFalse(leg.source_transaction_id)
        payment = leg._create_payment()
        self.assertTrue(payment)
        self.assertEqual(payment.amount, 60.0)


@tagged("post_install", "-at_install")
class TestGiftcardRedirectPbnkEndToEnd(_GiftcardTestBase):
    """Redirect mode: a giftcard slice plus its iDEAL remainder push each
    surface as an independent, refundable ``account.payment`` on the order,
    each refundable for its own full amount. Regression for order S00223
    (giftcard leg had ``payment_id=False`` and was invisible/unrefundable in
    Odoo, and once given a PBNK its refundable amount was understated by the
    remainder)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.giftcard.buckaroo_official_giftcard_method = "redirect"

    def test_redirect_giftcard_slice_and_remainder_each_get_refundable_payment(self):
        gc_tx = self._gc_tx("TX-GC-E2E-001", 60.0, payment_method=self.brand_vvv)
        gc_tx.provider_reference = "E2E_GC_KEY"
        gc_tx.buckaroo_official_service_code = "vvvgiftcard"
        gc_tx._set_done()

        remainder_push = parsed_from_form(
            {
                "brq_invoicenumber": gc_tx.reference,
                "brq_amount": "40.00",
                "brq_currency": "EUR",
                "brq_statuscode": "190",
                "brq_transactions": "E2E_IDEAL_KEY",
                "brq_transaction_method": "ideal",
                "brq_relatedtransaction_partialpayment": "E2E_GC_KEY",
            }
        )
        remainder_tx = gc_tx._buckaroo_split_remainder_push(remainder_push)

        self.assertTrue(remainder_tx)
        self.assertNotEqual(remainder_tx, gc_tx)
        self.assertEqual(remainder_tx.amount, 40.0)
        self.assertEqual(remainder_tx.payment_method_id, self.ideal)
        # The remainder must NOT be linked as a child of the giftcard tx, or
        # Odoo would treat it as a refund of the giftcard payment and understate
        # the giftcard leg's amount_available_for_refund (regression: it showed
        # 60 - 40 = 20, which also blocked full credit-note refunds).
        self.assertFalse(remainder_tx.source_transaction_id)
        remainder_tx.provider_reference = "E2E_IDEAL_KEY"
        remainder_tx._set_done()
        self.assertEqual(len(self.order.transaction_ids), 2)

        # Each leg becomes an independent, refundable account.payment, each
        # refundable for its own full amount.
        gc_payment = gc_tx._create_payment()
        remainder_payment = remainder_tx._create_payment()
        self.assertEqual(gc_payment.amount, 60.0)
        self.assertEqual(remainder_payment.amount, 40.0)
        self.assertFalse(remainder_payment.source_payment_id)
        self.assertEqual(gc_payment.amount_available_for_refund, 60.0)
        self.assertEqual(remainder_payment.amount_available_for_refund, 40.0)

        # Refunding the giftcard leg must target the giftcard's own key, not
        # the iDEAL remainder key. (If the remainder were a child of the
        # giftcard tx, _buckaroo_resolve_original_transaction_key would pick the
        # iDEAL key as a "capture child" and misroute the refund.)
        self.assertEqual(
            gc_tx.payment_method_id._buckaroo_resolve_original_transaction_key(gc_tx),
            gc_tx.provider_reference,
        )


@tagged("post_install", "-at_install")
class TestGiftcardGroupRemainder(_GiftcardTestBase):
    """Inline giftcard remainder settles into the Buckaroo group via a
    PayRemainder against the captured group transaction key — giftcard +
    remainder reconcile as one group transaction, with the shopper staying on
    the merchant checkout instead of being sent to Buckaroo's hosted page."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.giftcard.buckaroo_official_giftcard_method = "inline"

    def test_extract_group_key_reads_pay_remainder_details(self):
        response = _make_partial_response(consumed=5.00, group_key="GROUP-KEY-1")
        self.assertEqual(
            self.brand_vvv._buckaroo_extract_group_transaction_key(response),
            "GROUP-KEY-1",
        )

    def test_extract_group_key_returns_none_without_details(self):
        response = _make_partial_response(consumed=5.00)
        self.assertIsNone(self.brand_vvv._buckaroo_extract_group_transaction_key(response))

    def test_extract_group_key_falls_back_to_related_transaction(self):
        # No PayRemainderDetails, but a RelatedTransactions entry carries the key.
        response = _make_partial_response(consumed=5.00, related_key="REL-KEY-1")
        self.assertEqual(
            self.brand_vvv._buckaroo_extract_group_transaction_key(response),
            "REL-KEY-1",
        )

    def test_inline_partial_stores_group_key_on_order(self):
        tx = self._create_buckaroo_tx(
            reference="TX-GC-GROUP-001",
            amount=self.order_total,
            payment_method=self.brand_vvv,
        ).with_context(
            buckaroo_giftcard_cardnumber="VVV-PART-001",
            buckaroo_giftcard_pin="1234",
        )
        tx.sale_order_ids = [Command.set(self.order.ids)]
        response = _make_partial_response(consumed=5.00, group_key="GROUP-KEY-2")
        mock_builder = MagicMock()
        mock_builder.pay.return_value = response
        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method_giftcard.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            tx._get_specific_processing_values({})
        self.assertEqual(self.order.buckaroo_official_group_transaction_key, "GROUP-KEY-2")

    def test_remainder_group_key_empty_for_giftcard_leg(self):
        self.order.buckaroo_official_group_transaction_key = "GROUP-KEY-3"
        gc_tx = self._gc_tx("TX-GC-GROUP-GC", 5.00, payment_method=self.brand_vvv)
        self.assertFalse(gc_tx._buckaroo_giftcard_remainder_group_key())

    def test_remainder_group_key_returned_for_remainder_leg(self):
        self.order.buckaroo_official_group_transaction_key = "GROUP-KEY-4"
        rem_tx = self._gc_tx("TX-GC-GROUP-REM", 60.0, payment_method=self.ideal)
        self.assertEqual(rem_tx._buckaroo_giftcard_remainder_group_key(), "GROUP-KEY-4")

    def test_remainder_leg_creates_pay_remainder_with_group_key(self):
        self.order.buckaroo_official_group_transaction_key = "GROUP-KEY-5"
        rem_tx = self._gc_tx("TX-GC-GROUP-PR", 60.0, payment_method=self.ideal)

        mock_builder = MagicMock()
        client = MagicMock()
        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.ideal._buckaroo_create_payment(rem_tx, client)

        mock_builder.pay_remainder.assert_called_once_with(original_transaction_key="GROUP-KEY-5")
        mock_builder.pay.assert_not_called()

    def test_order_without_group_key_uses_plain_pay(self):
        plain_tx = self._gc_tx("TX-GC-GROUP-PLAIN", 60.0, payment_method=self.ideal)

        mock_builder = MagicMock()
        client = MagicMock()
        with patch(
            "odoo.addons.payment_buckaroo_official.models.payment_method.PaymentService"
        ) as MockPS:
            MockPS.return_value.create_payment.return_value = mock_builder
            self.ideal._buckaroo_create_payment(plain_tx, client)

        mock_builder.pay.assert_called_once()
        mock_builder.pay_remainder.assert_not_called()
