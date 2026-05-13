# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Tests that the transaction model dispatches to payment.method model methods.

Verifies that _get_specific_processing_values, _send_refund_request,
_send_capture_request, and _send_void_request call the _buckaroo_* methods
on payment_method_id.
"""

from unittest.mock import patch

from buckaroo.http.client import BuckarooApiError

from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.tests import tagged

from .common import BuckarooOfficialCommon, make_mock_sdk_response


def _make_mock_sdk_response(
    redirect_url="https://checkout.buckaroo.nl/pay/123", key="TXN_KEY_123", status_code=190
):
    """Build a mock SDK response with extra attributes for transaction tests."""
    resp = make_mock_sdk_response(status_code)
    resp.get_redirect_url.return_value = redirect_url
    resp.key = key
    resp.redirect_url = redirect_url
    resp.required_action = None
    resp.buckaroo_status_message = "Success"
    resp._raw_data = {
        "data": {
            "Key": key,
            "Status": {
                "Code": {"Code": status_code, "Description": "OK"},
            },
        },
    }
    return resp


@tagged("post_install", "-at_install")
class TestTransactionModelDispatch(BuckarooOfficialCommon):
    """Transaction model should call payment_method_id._buckaroo_* methods."""

    def _create_transaction(self, **overrides):
        vals = dict(self.buckaroo_tx_values, **overrides)
        return self.env["payment.transaction"].create(vals)

    def test_processing_values_calls_model_create_payment(self):
        """_get_specific_processing_values calls payment_method_id._buckaroo_create_payment."""
        tx = self._create_transaction(payment_method_id=self.ideal.id)
        mock_response = _make_mock_sdk_response()

        with (
            patch.object(
                type(self.ideal),
                "_buckaroo_create_payment",
                return_value=mock_response,
            ) as mock_create,
            patch.object(
                type(self.ideal),
                "_buckaroo_extract_redirect_url",
                return_value="https://checkout.buckaroo.nl/pay/123",
            ) as mock_extract,
        ):
            result = tx._get_specific_processing_values({})

        mock_create.assert_called_once()
        mock_extract.assert_called_once()
        self.assertEqual(result["api_url"], "https://checkout.buckaroo.nl/pay/123")

    def test_refund_calls_model_create_refund(self):
        """_send_refund_request calls payment_method_id._buckaroo_create_refund."""
        source_tx = self._create_transaction(payment_method_id=self.ideal.id)
        source_tx._apply_updates(self._get_buckaroo_callback_data(status_code=190))
        self.assertEqual(source_tx.state, "done")

        mock_response = _make_mock_sdk_response(key="REFUND_KEY")

        with patch.object(
            type(self.ideal),
            "_buckaroo_create_refund",
            return_value=mock_response,
        ) as mock_refund:
            refund_tx = source_tx._refund()

        mock_refund.assert_called_once()
        # Verify the refund tx was created
        self.assertTrue(refund_tx.exists())
        self.assertEqual(refund_tx.operation, "refund")

    def test_capture_calls_model_create_capture(self):
        """_capture() dispatches to payment_method_id._buckaroo_create_capture."""
        creditcard = self.env.ref("payment_buckaroo_official.payment_method_creditcard")
        self.buckaroo.payment_method_ids = [Command.link(creditcard.id)]
        creditcard.write({"buckaroo_official_creditcard_authorize": "authorize"})

        tx = self._create_transaction(payment_method_id=creditcard.id)
        tx.buckaroo_official_payment_action = "authorize"
        tx._apply_updates(self._get_buckaroo_callback_data(status_code=190))
        self.assertEqual(tx.state, "authorized")

        mock_response = _make_mock_sdk_response(key="CAPTURE_KEY")

        with patch.object(
            type(creditcard),
            "_buckaroo_create_capture",
            return_value=mock_response,
        ) as mock_capture:
            capture_tx = tx._capture()

        mock_capture.assert_called_once()
        self.assertEqual(capture_tx.state, "done")

    def test_void_calls_model_create_void(self):
        """_void() dispatches to payment_method_id._buckaroo_create_void."""
        creditcard = self.env.ref("payment_buckaroo_official.payment_method_creditcard")
        self.buckaroo.payment_method_ids = [Command.link(creditcard.id)]
        creditcard.write({"buckaroo_official_creditcard_authorize": "authorize"})

        tx = self._create_transaction(payment_method_id=creditcard.id)
        tx.buckaroo_official_payment_action = "authorize"
        tx._apply_updates(self._get_buckaroo_callback_data(status_code=190))
        self.assertEqual(tx.state, "authorized")

        mock_response = _make_mock_sdk_response(key="VOID_KEY")

        with patch.object(
            type(creditcard),
            "_buckaroo_create_void",
            return_value=mock_response,
        ) as mock_void:
            void_tx = tx._void()

        mock_void.assert_called_once()
        self.assertEqual(void_tx.state, "cancel")

    def test_processing_values_wraps_buckaroo_api_error_as_validation_error(self):
        """_get_specific_processing_values surfaces SDK BuckarooApiError as ValidationError."""
        tx = self._create_transaction(payment_method_id=self.ideal.id)
        sdk_error = BuckarooApiError("Buckaroo API returned status 500")

        with patch.object(
            type(self.ideal),
            "_buckaroo_create_payment",
            side_effect=sdk_error,
        ):
            with self.assertRaises(ValidationError) as cm:
                tx._get_specific_processing_values({})

        self.assertIn("payment request failed", str(cm.exception))
        self.assertIn("status 500", str(cm.exception))

    def test_send_sdk_request_wraps_buckaroo_api_error_as_validation_error(self):
        """_buckaroo_official_send_sdk_request surfaces SDK BuckarooApiError as ValidationError."""
        tx = self._create_transaction(payment_method_id=self.ideal.id)
        sdk_error = BuckarooApiError("Buckaroo API returned status 503")

        def failing_call(_client):
            raise sdk_error

        with self.assertRaises(ValidationError) as cm:
            tx._buckaroo_official_send_sdk_request("refund", failing_call)

        self.assertIn("refund request failed", str(cm.exception))
        self.assertIn("status 503", str(cm.exception))

    # -- Idempotency guards: outbound-side state check --

    def _make_done_source_tx(self, payment_method=None):
        """Create a source tx already in 'done' state, suitable for refund tests."""
        pm = payment_method or self.ideal
        tx = self._create_transaction(payment_method_id=pm.id)
        tx._apply_updates(self._get_buckaroo_callback_data(status_code=190))
        self.assertEqual(tx.state, "done")
        return tx

    def test_refund_is_idempotent_while_in_flight(self):
        """Double-submit of a refund must POST once, not twice — pending blocks re-entry."""
        source_tx = self._make_done_source_tx()
        mock_response = _make_mock_sdk_response(key="REFUND_KEY", status_code=791)

        # First call: state is draft → SDK gets called and tx moves to pending.
        with patch.object(
            type(self.ideal),
            "_buckaroo_create_refund",
            return_value=mock_response,
        ) as mock_refund:
            refund_tx = source_tx._refund()

        self.assertEqual(mock_refund.call_count, 1)
        self.assertEqual(refund_tx.state, "pending")

        # Second call on the same refund tx: state is pending → SDK must NOT be called again.
        with patch.object(
            type(self.ideal),
            "_buckaroo_create_refund",
            return_value=mock_response,
        ) as mock_refund_retry:
            refund_tx._send_refund_request()

        self.assertEqual(mock_refund_retry.call_count, 0)

    def test_refund_is_idempotent_after_completion(self):
        """A refund tx already in 'done' must not re-POST to Buckaroo."""
        source_tx = self._make_done_source_tx()
        mock_response = _make_mock_sdk_response(key="REFUND_KEY", status_code=190)

        with patch.object(
            type(self.ideal),
            "_buckaroo_create_refund",
            return_value=mock_response,
        ):
            refund_tx = source_tx._refund()
        self.assertEqual(refund_tx.state, "done")

        with patch.object(
            type(self.ideal),
            "_buckaroo_create_refund",
            return_value=mock_response,
        ) as mock_refund_retry:
            refund_tx._send_refund_request()

        self.assertEqual(mock_refund_retry.call_count, 0)

    def test_refund_allows_retry_after_transient_failure(self):
        """A refund tx in 'error' is retry-able: guard must allow a second SDK call."""
        source_tx = self._make_done_source_tx()
        # First attempt errors (status code 490 = Failed).
        failed_response = _make_mock_sdk_response(key="REFUND_ERR", status_code=490)

        with patch.object(
            type(self.ideal),
            "_buckaroo_create_refund",
            return_value=failed_response,
        ):
            refund_tx = source_tx._refund()
        self.assertEqual(refund_tx.state, "error")

        # Retry: state=error is a legitimate retry window.
        success_response = _make_mock_sdk_response(key="REFUND_OK", status_code=190)
        with patch.object(
            type(self.ideal),
            "_buckaroo_create_refund",
            return_value=success_response,
        ) as mock_refund_retry:
            refund_tx._send_refund_request()

        self.assertEqual(mock_refund_retry.call_count, 1)
        self.assertEqual(refund_tx.state, "done")

    def test_capture_is_idempotent_after_completion(self):
        """Double-submit of capture must not re-POST once the tx has moved past draft."""
        creditcard = self.env.ref("payment_buckaroo_official.payment_method_creditcard")
        self.buckaroo.payment_method_ids = [Command.link(creditcard.id)]
        creditcard.write({"buckaroo_official_creditcard_authorize": "authorize"})

        tx = self._create_transaction(payment_method_id=creditcard.id)
        tx.buckaroo_official_payment_action = "authorize"
        tx._apply_updates(self._get_buckaroo_callback_data(status_code=190))
        self.assertEqual(tx.state, "authorized")

        mock_response = _make_mock_sdk_response(key="CAPTURE_KEY", status_code=190)
        with patch.object(
            type(creditcard),
            "_buckaroo_create_capture",
            return_value=mock_response,
        ):
            capture_tx = tx._capture()
        self.assertEqual(capture_tx.state, "done")

        # Second submit on the same capture tx: no SDK call.
        with patch.object(
            type(creditcard),
            "_buckaroo_create_capture",
            return_value=mock_response,
        ) as mock_capture_retry:
            capture_tx._send_capture_request()

        self.assertEqual(mock_capture_retry.call_count, 0)

    def test_void_is_idempotent_after_completion(self):
        """Double-submit of void must not re-POST once the tx has moved past draft."""
        creditcard = self.env.ref("payment_buckaroo_official.payment_method_creditcard")
        self.buckaroo.payment_method_ids = [Command.link(creditcard.id)]
        creditcard.write({"buckaroo_official_creditcard_authorize": "authorize"})

        tx = self._create_transaction(payment_method_id=creditcard.id)
        tx.buckaroo_official_payment_action = "authorize"
        tx._apply_updates(self._get_buckaroo_callback_data(status_code=190))
        self.assertEqual(tx.state, "authorized")

        mock_response = _make_mock_sdk_response(key="VOID_KEY", status_code=190)
        with patch.object(
            type(creditcard),
            "_buckaroo_create_void",
            return_value=mock_response,
        ):
            void_tx = tx._void()
        self.assertEqual(void_tx.state, "cancel")

        with patch.object(
            type(creditcard),
            "_buckaroo_create_void",
            return_value=mock_response,
        ) as mock_void_retry:
            void_tx._send_void_request()

        self.assertEqual(mock_void_retry.call_count, 0)
