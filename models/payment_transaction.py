# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

from buckaroo.http.client import BuckarooApiError
from werkzeug.urls import url_decode, url_parse

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..utils import const

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    buckaroo_official_service_code = fields.Char(
        string="Buckaroo Service Code",
        help="Service code from Buckaroo (e.g. 'visa', 'mastercard'). "
             "Stored from the initial transaction response for use in capture/void.",
    )
    buckaroo_official_payment_action = fields.Selection(
        selection=[('pay', "Pay"), ('authorize', "Authorize")],
        string="Buckaroo Payment Action",
        help="Frozen at creation time so push notifications use a stable value.",
    )

    def _get_specific_processing_values(self, processing_values):
        if self.provider_code != const.PROVIDER_CODE:
            return super()._get_specific_processing_values(processing_values)

        if not self.payment_method_id._buckaroo_official_is_amount_compatible(
            self.amount, set(self.provider_id.ids)
        ):
            raise ValidationError(
                _(
                    "Buckaroo payment method %(payment_method)s is not available for this "
                    "order total.",
                    payment_method=self.payment_method_id.name,
                )
            )

        # Freeze the action so _apply_updates uses a stable value even
        # if the merchant flips the setting between checkout and push.
        action = self.payment_method_id._buckaroo_get_payment_action()
        if action == 'authorize':
            self.buckaroo_official_payment_action = 'authorize'

        client = self._buckaroo_official_get_client()
        try:
            response = self.payment_method_id._buckaroo_create_payment(self, client)
        except BuckarooApiError as e:
            raise ValidationError(
                _("Buckaroo: payment request failed: %s", e)
            )

        _logger.debug(
            "Buckaroo response for %s: status_code=%s, key=%s, redirect_url=%s, "
            "required_action=%s, raw_data=%s",
            self.reference, response.status_code, response.key,
            response.redirect_url, response.required_action,
            response._raw_data,
        )

        redirect_url = self.payment_method_id._buckaroo_extract_redirect_url(response)
        if not redirect_url:
            error_message = response.get_some_error()
            if error_message:
                raise ValidationError(_("Buckaroo: %s", error_message))
            raise ValidationError(_(
                "Buckaroo: payment could not be initiated (no redirect URL "
                "received and no error message provided)."
            ))

        self.provider_reference = response.key
        return {'api_url': redirect_url}

    def _buckaroo_official_get_client(self):
        return self.provider_id._buckaroo_official_get_client()

    def _buckaroo_official_handle_response_status(self, response, operation,
                                                    success_state='done'):
        """Map a Buckaroo SDK ``PaymentResponse`` to a tx state.

        Strict equality against ``BuckarooStatusCode.SUCCESS`` for the
        done leg — SDK's ``is_successful`` reads ``is_successful_payment``
        from raw_data and isn't reliable for refund / capture / void.
        """
        status_code = (
            response.status.code.code
            if response.status and response.status.code
            else None
        )
        if status_code is None:
            self._set_error("Buckaroo: %s returned no status code" % operation)
        elif status_code == const.BuckarooStatusCode.SUCCESS:
            if success_state == 'canceled':
                self._set_canceled()
            else:
                self._set_done()
        elif response.is_pending():
            self._set_pending()
        elif response.is_cancelled():
            self._set_canceled()
        else:
            self._set_error(
                "Buckaroo: %s failed with status code: %s" % (operation, status_code)
            )

    def _buckaroo_official_send_sdk_request(self, operation, sdk_call,
                                              success_state='done'):
        """Run *sdk_call(client)*, log, persist key, dispatch state."""
        client = self._buckaroo_official_get_client()
        try:
            response = sdk_call(client)
        except BuckarooApiError as e:
            raise ValidationError(
                _("Buckaroo: %(operation)s request failed: %(error)s",
                  operation=operation, error=e)
            )

        _logger.info(
            "Buckaroo %s response for %s: status_code=%s, key=%s",
            operation, self.reference, response.status_code, response.key,
        )

        if response.key:
            self.provider_reference = response.key

        self._buckaroo_official_handle_response_status(
            response, operation, success_state=success_state,
        )

    def _buckaroo_official_is_dispatchable(self, operation):
        """``True`` when the tx may POST to Buckaroo; ``False`` to skip."""
        if self.state in ('draft', 'error'):
            return True
        _logger.info(
            "Skipping Buckaroo %s for transaction %s: state=%s (already dispatched)",
            operation, self.reference, self.state,
        )
        return False

    def _send_refund_request(self):
        if self.provider_code != const.PROVIDER_CODE:
            return super()._send_refund_request()

        if not self._buckaroo_official_is_dispatchable('refund'):
            return

        source_tx = self.source_transaction_id
        self._buckaroo_official_send_sdk_request(
            'refund',
            lambda client: self.payment_method_id._buckaroo_create_refund(
                source_tx, self, client,
            ),
        )

    def _send_capture_request(self):
        if self.provider_code != const.PROVIDER_CODE:
            return super()._send_capture_request()

        if not self._buckaroo_official_is_dispatchable('capture'):
            return

        self._buckaroo_official_send_sdk_request(
            'capture',
            lambda client: self.payment_method_id._buckaroo_create_capture(self, client),
        )

    def _send_void_request(self):
        if self.provider_code != const.PROVIDER_CODE:
            return super()._send_void_request()

        if not self._buckaroo_official_is_dispatchable('void'):
            return

        self._buckaroo_official_send_sdk_request(
            'void',
            lambda client: self.payment_method_id._buckaroo_create_void(self, client),
            success_state='canceled',
        )

    def _get_specific_rendering_values(self, processing_values):
        if self.provider_code != const.PROVIDER_CODE:
            return super()._get_specific_rendering_values(processing_values)

        api_url = processing_values.get('api_url', '')
        parsed = url_parse(api_url)
        base_url = parsed.replace(query='').to_url()
        url_params = url_decode(parsed.query)

        return {
            'api_url': base_url,
            'url_params': url_params,
        }

    @api.model
    def _extract_reference(self, provider_code, payment_data):
        if provider_code != const.PROVIDER_CODE:
            return super()._extract_reference(provider_code, payment_data)

        ref = payment_data.reference
        if not ref:
            raise ValidationError("Buckaroo: missing transaction reference in callback data.")
        return ref

    def _extract_amount_data(self, payment_data):
        """Returns ``None`` to skip framework amount/currency validation —
        Klarna's "Reserve failed" pushes carry no amount or currency, and
        returning ``{}`` would crash the framework's ``amount`` lookup.
        """
        if self.provider_code != const.PROVIDER_CODE:
            return super()._extract_amount_data(payment_data)

        credit_amount = payment_data.credit_amount
        amount = payment_data.amount or (abs(credit_amount) if credit_amount else None)
        currency = payment_data.currency
        if amount is None or not currency:
            return None
        return {
            'amount': amount,
            'currency_code': currency,
        }

    def _apply_updates(self, payment_data):
        if self.provider_code != const.PROVIDER_CODE:
            return super()._apply_updates(payment_data)

        if self.state in ('done', 'cancel', 'error'):
            _logger.info(
                "Skipping duplicate Buckaroo callback for transaction %s (state=%s)",
                self.reference, self.state,
            )
            return

        txn_key = payment_data.transaction_key
        if txn_key:
            self.provider_reference = txn_key

        service_code = payment_data.service_code
        if service_code:
            self.buckaroo_official_service_code = service_code

        status_code = payment_data.status_code
        if status_code is None:
            self._set_error("Buckaroo: received invalid status code")
            return

        if payment_data.is_success():
            if self.buckaroo_official_payment_action == 'authorize':
                self._set_authorized()
            else:
                self._set_done()
        elif payment_data.is_pending():
            self._set_pending()
        elif payment_data.is_cancelled():
            self._set_canceled()
        elif payment_data.is_failed():
            self._set_error("Buckaroo: payment failed with status code: %s" % status_code)
        else:
            self._set_error("Buckaroo: unhandled status code: %s" % status_code)
