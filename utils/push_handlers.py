# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json
import logging
from dataclasses import dataclass

from buckaroo.services.reply import HttpPost, Json
from werkzeug.exceptions import Forbidden

from . import const

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedPush:
    """Parsed Buckaroo push / return data in a single wire-format-agnostic shape."""

    reference: str | None
    amount: float | None
    credit_amount: float | None
    currency: str | None
    status_code: int | None
    transaction_key: str | None
    service_code: str | None
    signature: str | None
    raw: dict
    is_json: bool = False
    auth_header: str | None = None
    uri: str | None = None
    method: str | None = None
    body_bytes: bytes | None = None

    def is_success(self):
        return self._check('done')

    def is_pending(self):
        return self._check('pending')

    def is_cancelled(self):
        return self._check('cancel')

    def is_failed(self):
        return self._check('error')

    def _check(self, group):
        return (
            self.status_code is not None
            and self.status_code in const.BUCKAROO_STATUS_CODES_MAPPING[group]
        )


def parse_push(request):
    """Sniff content-type, parse wire format, return a :class:`ParsedPush`.

    Signature is NOT yet verified. Callers must invoke
    :func:`verify_signature` after resolving the transaction's provider.
    """
    content_type = request.httprequest.content_type or ''
    if 'application/json' in content_type:
        return _parse_json(request)
    return _parse_form(request)


def verify_signature(parsed, provider):
    """Raise :class:`werkzeug.exceptions.Forbidden` if signature missing or invalid."""
    if parsed.is_json:
        if not parsed.auth_header:
            _logger.warning("Received Buckaroo Official JSON push with missing Authorization header")
            raise Forbidden()
        handler = Json(
            provider.buckaroo_official_website_key,
            provider.buckaroo_official_secret_key,
        )
        if not handler.validate(
            parsed.auth_header, parsed.uri, parsed.method, parsed.body_bytes,
        ):
            _logger.warning("Received Buckaroo Official JSON push with invalid HMAC")
            raise Forbidden()
        return
    if not parsed.signature:
        _logger.warning("Received Buckaroo Official form push with missing signature")
        raise Forbidden()
    if not HttpPost(provider.buckaroo_official_secret_key).validate(parsed.raw):
        _logger.warning("Received Buckaroo Official form push with invalid signature")
        raise Forbidden()


def _parse_form(request):
    raw = dict(request.httprequest.values)
    data = {k.lower(): v for k, v in raw.items()}
    amount = data.get('brq_amount')
    credit = data.get('brq_amount_credit')
    status_code_raw = data.get('brq_statuscode', '')
    try:
        status_code = int(status_code_raw) if status_code_raw else None
    except ValueError:
        status_code = None
    return ParsedPush(
        reference=data.get('brq_invoicenumber') or data.get('brq_description'),
        amount=float(amount) if amount else None,
        credit_amount=float(credit) if credit else None,
        currency=data.get('brq_currency'),
        status_code=status_code,
        transaction_key=data.get('brq_transactions'),
        service_code=data.get('brq_transaction_method') or data.get('brq_payment_method'),
        signature=data.get('brq_signature'),
        raw=raw,
    )


def _parse_json(request):
    body_bytes = request.httprequest.get_data() or b''
    try:
        payload = json.loads(body_bytes) if body_bytes else {}
    except (json.JSONDecodeError, TypeError):
        _logger.warning("Buckaroo JSON push: invalid JSON body")
        payload = {}
    data = payload.get('Transaction', payload) if isinstance(payload, dict) else {}

    status = data.get('Status') or {}
    code_obj = status.get('Code') or {}
    code = code_obj.get('Code') if isinstance(code_obj, dict) else code_obj
    status_code = int(code) if code is not None else None

    service_code = None
    services = data.get('Services') or []
    if services and isinstance(services, list):
        for svc in services:
            name = svc.get('Name') if isinstance(svc, dict) else None
            if name:
                service_code = name
                break
    if not service_code:
        service_code = data.get('ServiceCode')

    amount = data.get('AmountDebit')
    credit = data.get('AmountCredit')

    headers = getattr(request.httprequest, 'headers', None)
    auth_header = headers.get('Authorization') if headers else None
    uri = getattr(request.httprequest, 'url', None)
    method = getattr(request.httprequest, 'method', None)

    return ParsedPush(
        reference=data.get('Invoice') or data.get('Description'),
        amount=float(amount) if amount is not None else None,
        credit_amount=float(credit) if credit is not None else None,
        currency=data.get('Currency'),
        status_code=status_code,
        transaction_key=data.get('Key'),
        service_code=service_code,
        signature=None,
        raw=payload if isinstance(payload, dict) else {},
        is_json=True,
        auth_header=auth_header,
        uri=uri,
        method=method,
        body_bytes=body_bytes or b'',
    )
