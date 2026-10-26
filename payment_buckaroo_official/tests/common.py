# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json as _json
from unittest.mock import MagicMock, patch

from odoo.addons.payment.tests.common import PaymentCommon

from ..utils.const import BUCKAROO_STATUS_CODES_MAPPING
from ..utils.push_handlers import parse_push


def make_mock_request(content_type="application/x-www-form-urlencoded", body=None, values=None):
    """Build a minimal mock ``odoo.http.request`` for testing push parsing."""
    req = MagicMock()
    req.httprequest.content_type = content_type
    req.httprequest.headers = {}
    req.httprequest.url = None
    req.httprequest.method = None
    if body is not None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        req.httprequest.get_data.return_value = body
    if values is not None:
        req.httprequest.values = values
    return req


def parsed_from_form(raw):
    """Build a :class:`ParsedPush` from a raw ``brq_*`` dict via the production parser."""
    return parse_push(
        make_mock_request(
            content_type="application/x-www-form-urlencoded",
            values=raw,
        )
    )


def parsed_from_json(payload):
    """Build a :class:`ParsedPush` from a Buckaroo JSON-push payload via the production parser."""
    return parse_push(
        make_mock_request(
            content_type="application/json",
            body=_json.dumps(payload),
        )
    )


def make_mock_response(redirect_url="https://checkout.buckaroo.nl/pay/123"):
    """Build a mock SDK response with standard attributes."""
    resp = MagicMock()
    resp.get_redirect_url.return_value = redirect_url
    resp.key = "TXN_KEY_123"
    resp.required_action = None
    return resp


def make_mock_sdk_builder():
    """Return (mock_builder, mock_response) for patching PaymentService.

    The builder has stubs for pay, refund, authorize, reserve, payWithToken,
    authorizeWithToken, capture, cancelAuthorize, cancelReservation, and
    execute_action.
    """
    mock_response = make_mock_response()
    mock_builder = MagicMock()
    mock_builder.pay.return_value = mock_response
    mock_builder.refund.return_value = mock_response
    mock_builder.authorize.return_value = mock_response
    mock_builder.reserve.return_value = mock_response
    mock_builder.payWithToken.return_value = mock_response
    mock_builder.authorizeWithToken.return_value = mock_response
    mock_builder.capture.return_value = mock_response
    mock_builder.cancelAuthorize.return_value = mock_response
    mock_builder.cancelReservation.return_value = mock_response
    mock_builder.execute_action.return_value = mock_response
    return mock_builder, mock_response


# Buckaroo status codes per documented group
# (https://docs.buckaroo.io/docs/statuscodes), reused by all BNPL push
# tests so adding a new code is a one-line change.
CALLBACK_DONE_CODES = [190]
CALLBACK_PENDING_CODES = [790, 791, 792, 793]
CALLBACK_CANCEL_CODES = [890, 891]
CALLBACK_ERROR_CODES = [490, 690]

# (status_code, expected_refund_state) — reused across BNPL refund
# matrices (Billink, Klarna, Riverty).
REFUND_STATUS_CASES = [
    (190, "done"),
    (790, "pending"),
    (890, "cancel"),
    (490, "error"),
]


def make_mock_sdk_response(status_code):
    """Build a mock SDK response with status flags based on *status_code*.

    Sets is_successful/is_pending/is_cancelled/is_failed using the
    BUCKAROO_STATUS_CODES_MAPPING constant.
    """
    response = MagicMock()
    response.key = "MOCK_KEY_%s" % status_code
    response.status_code = 200

    mock_sc = MagicMock()
    mock_sc.code = status_code
    mock_st = MagicMock()
    mock_st.code = mock_sc
    response.status = mock_st

    response.is_successful.return_value = status_code in BUCKAROO_STATUS_CODES_MAPPING["done"]
    response.is_pending.return_value = status_code in BUCKAROO_STATUS_CODES_MAPPING["pending"]
    response.is_cancelled.return_value = status_code in BUCKAROO_STATUS_CODES_MAPPING["cancel"]
    response.is_failed.return_value = status_code in BUCKAROO_STATUS_CODES_MAPPING["error"]
    return response


class BuckarooOfficialCommon(PaymentCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.buckaroo = cls._prepare_provider(
            "buckaroo_official",
            update_values={
                "buckaroo_official_website_key": "test_website_key",
                "buckaroo_official_secret_key": "test_secret_key",
            },
        )
        # Odoo 20 no longer forces test mode in ``_prepare_provider``.
        cls.buckaroo.write({"is_live": False, "active": True})
        # Payment methods belong to one provider in Odoo 20: resolve them on
        # the test provider instead of by xmlid.
        cls.ideal = cls._pm("buckaroo_ideal")
        assert cls.ideal, "The Buckaroo test provider has no iDEAL payment method"

        # Run the real processing logic: 19 never mocked ``_process``.
        cls.enable_process_patcher = False

        cls.buckaroo_tx_values = {
            "provider_id": cls.buckaroo.id,
            "payment_method_id": cls.ideal.id,
            "reference": "TX-BUCK-001",
            "amount": 50.00,
            "currency_id": cls.currency_euro.id,
            "partner_id": cls.partner.id,
            "operation": "online_redirect",
        }

    def setUp(self):
        super().setUp()
        # Queued payloads must be plain JSON. Fail fast on anything else (e.g.
        # an unconfigured MagicMock SDK response) instead of letting the Json
        # field stringify it recursively.
        PaymentTransaction = self.registry["payment.transaction"]
        record = PaymentTransaction._record

        def _strict_record(tx, payment_data):
            _json.dumps(payment_data)
            return record(tx, payment_data)

        self.startPatcher(patch.object(PaymentTransaction, "_record", _strict_record))

    @classmethod
    def _pm(cls, code):
        """Return the Buckaroo test provider's payment method with *code*."""
        return cls.buckaroo._get_pm_from_code(code)

    def _route_push(self, parsed):
        """Drive the webhook controller path for *parsed*: resolve the tx,
        split a remainder push, queue the push and run the processing cron.
        Returns the transaction the push was recorded on."""
        tx_sudo = (
            self.env["payment.transaction"].sudo()._search_by_reference("buckaroo_official", parsed)
        )
        if not tx_sudo:
            return None
        remainder_tx = tx_sudo._buckaroo_split_remainder_push(parsed)
        target = remainder_tx or tx_sudo
        target._record(parsed.to_payload())
        self._run_processing()
        return target

    def _run_processing(self):
        """Run the processing cron and fail on any payload it could not
        process: the cron only logs those and flags them as errored."""
        PaymentData = self.env["payment.data"].sudo()
        known_errors = PaymentData.search([("errored", "=", True)])
        super()._run_processing()
        self.env.invalidate_all()
        errored = PaymentData.search([("errored", "=", True)]) - known_errors
        if errored:
            self.fail("Payment data processing failed:\n%s" % errored[:1].error_traceback)

    def _process_push(self, parsed, tx=None):
        """Process *parsed* synchronously, as the processing cron does, on *tx*
        or on the transaction its reference resolves to. Returns the tx."""
        if tx is None:
            tx = (
                self.env["payment.transaction"]
                .sudo()
                ._search_by_reference("buckaroo_official", parsed)
            )
        tx.with_context(payment_safe_write=True)._process(parsed.to_payload())
        return tx

    def _create_buckaroo_tx(self, reference="TX-100", amount=50.0, payment_method=None):
        """Create a payment.transaction tied to the Buckaroo test provider.

        Uses ``self.ideal`` by default; pass *payment_method* to override.
        """
        pm = payment_method or self.ideal
        return self.env["payment.transaction"].create(
            {
                "provider_id": self.buckaroo.id,
                "payment_method_id": pm.id,
                "reference": reference,
                "amount": amount,
                "currency_id": self.currency_euro.id,
                "partner_id": self.partner.id,
                "operation": "online_redirect",
            }
        )

    @classmethod
    def _make_callback_raw(cls, status_code=190, **overrides):
        """Build the raw ``brq_*`` dict shared by the callback helpers."""
        raw = {
            "brq_invoicenumber": "TX-BUCK-001",
            "brq_amount": "50.00",
            "brq_currency": "EUR",
            "brq_statuscode": str(status_code),
            "brq_transactions": "BUCK_TXN_KEY_123",
        }
        raw.update(overrides)
        return raw

    @classmethod
    def _get_buckaroo_callback_data(cls, status_code=190, **overrides):
        """Return a :class:`ParsedPush` simulating Buckaroo callback data."""
        return parsed_from_form(cls._make_callback_raw(status_code, **overrides))
