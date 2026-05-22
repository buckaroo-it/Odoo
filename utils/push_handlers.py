# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json
import logging
from dataclasses import dataclass, field

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
    lowered_keys: dict = field(default_factory=dict)
    service_parameters: dict = field(default_factory=dict)
    is_json: bool = False
    auth_header: str | None = None
    uri: str | None = None
    method: str | None = None
    body_bytes: bytes | None = None

    def is_success(self):
        return self._check("done")

    def is_pending(self):
        return self._check("pending")

    def is_cancelled(self):
        return self._check("cancel")

    def is_failed(self):
        return self._check("error")

    def get_service_parameter(self, name):
        """Case-insensitive lookup over ``service_parameters``. Mirrors
        ``buckaroo.models.payment_response.PaymentResponse.get_service_parameter``
        so SDK responses and push payloads expose the same surface."""
        target = name.lower()
        for key, value in self.service_parameters.items():
            if key.lower() == target:
                return value
        return None

    def get_partial_payment_relation(self):
        """Return the source transaction key for a ``PartialPayment`` relation,
        or ``None``. Bridges form (``brq_relatedtransaction_partialpayment``)
        and JSON (``Transaction.RelatedTransactions[]``) push shapes so the
        remainder-split detector handles both wire formats uniformly."""
        if self.is_json:
            data = self.raw.get("Transaction", self.raw) if isinstance(self.raw, dict) else {}
            related = data.get("RelatedTransactions") or []
            for rel in related:
                if not isinstance(rel, dict):
                    continue
                if (rel.get("RelationType") or "").lower() == "partialpayment":
                    key = rel.get("RelatedTransactionKey")
                    if key:
                        return key
            return None
        return self.lowered_keys.get("brq_relatedtransaction_partialpayment") or None

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
    content_type = request.httprequest.content_type or ""
    if "application/json" in content_type:
        return _parse_json(request)
    return _parse_form(request)


def verify_signature(parsed, provider):
    """Raise :class:`werkzeug.exceptions.Forbidden` if signature missing or invalid."""
    if parsed.is_json:
        if not parsed.auth_header:
            _logger.warning(
                "Received Buckaroo Official JSON push with missing Authorization header"
            )
            raise Forbidden()
        handler = Json(
            provider.buckaroo_official_website_key,
            provider.buckaroo_official_secret_key,
        )
        if not handler.validate(
            parsed.auth_header,
            parsed.uri,
            parsed.method,
            parsed.body_bytes,
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
    amount = data.get("brq_amount")
    credit = data.get("brq_amount_credit")
    status_code_raw = data.get("brq_statuscode", "")
    try:
        status_code = int(status_code_raw) if status_code_raw else None
    except ValueError:
        status_code = None
    service_code = data.get("brq_transaction_method") or data.get("brq_payment_method")
    return ParsedPush(
        reference=data.get("brq_invoicenumber") or data.get("brq_description"),
        amount=float(amount) if amount else None,
        credit_amount=float(credit) if credit else None,
        currency=data.get("brq_currency"),
        status_code=status_code,
        transaction_key=data.get("brq_transactions"),
        service_code=service_code,
        signature=data.get("brq_signature"),
        raw=raw,
        lowered_keys=data,
        service_parameters=_extract_form_service_parameters(raw, service_code),
    )


def _extract_form_service_parameters(raw, service):
    """Flatten ``brq_SERVICE_<service>_<name>=<value>`` form keys scoped
    to *service* (the primary service code on this push). Scoping prevents
    cross-service collisions (e.g. a surcharge sub-service shipped on the
    same push overwriting the primary service's parameters) and tolerates
    service names that contain underscores."""
    if not service:
        return {}
    prefix = f"brq_service_{service.lower()}_"
    plen = len(prefix)
    out = {}
    for key, value in raw.items():
        if not value or len(key) <= plen:
            continue
        if key[:plen].lower() != prefix:
            continue
        out[key[plen:]] = value
    return out


def _parse_json(request):
    body_bytes = request.httprequest.get_data() or b""
    try:
        payload = json.loads(body_bytes) if body_bytes else {}
    except (json.JSONDecodeError, TypeError):
        _logger.warning("Buckaroo JSON push: invalid JSON body")
        payload = {}
    data = payload.get("Transaction", payload) if isinstance(payload, dict) else {}

    status = data.get("Status") or {}
    code_obj = status.get("Code") or {}
    code = code_obj.get("Code") if isinstance(code_obj, dict) else code_obj
    status_code = int(code) if code is not None else None

    primary_service = next(
        (s for s in (data.get("Services") or []) if isinstance(s, dict) and s.get("Name")),
        None,
    )
    service_parameters = {}
    if primary_service is not None:
        for param in primary_service.get("Parameters") or []:
            if not isinstance(param, dict):
                continue
            pname = param.get("Name")
            if pname:
                service_parameters[pname] = param.get("Value")
    service_code = primary_service["Name"] if primary_service else data.get("ServiceCode")

    amount = data.get("AmountDebit")
    credit = data.get("AmountCredit")

    headers = getattr(request.httprequest, "headers", None)
    auth_header = headers.get("Authorization") if headers else None
    uri = getattr(request.httprequest, "url", None)
    method = getattr(request.httprequest, "method", None)

    return ParsedPush(
        reference=data.get("Invoice") or data.get("Description"),
        amount=float(amount) if amount is not None else None,
        credit_amount=float(credit) if credit is not None else None,
        currency=data.get("Currency"),
        status_code=status_code,
        transaction_key=data.get("Key"),
        service_code=service_code,
        signature=None,
        raw=payload if isinstance(payload, dict) else {},
        service_parameters=service_parameters,
        is_json=True,
        auth_header=auth_header,
        uri=uri,
        method=method,
        body_bytes=body_bytes or b"",
    )
