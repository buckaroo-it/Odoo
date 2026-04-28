# Part of Odoo. See LICENSE file for full copyright and licensing details.

import re

from buckaroo.services.payment_service import PaymentService

from odoo import _, fields, models
from odoo.exceptions import ValidationError

from ..helpers.articles import get_order_articles
from ..helpers.customer import (
    get_billing_partner,
    get_customer_data,
    get_shipping_partner,
)


class PaymentMethodKlarna(models.Model):
    _inherit = 'payment.method'

    @staticmethod
    def _format_klarna_articles(articles):
        """Map generic article dicts to Klarna's API article format."""
        return [
            {
                'articleNumber': a['identifier'],
                'articleTitle': PaymentMethodKlarna._sanitize_text(a['description']),
                'articleQuantity': str(int(a['quantity'])),
                'articlePrice': a['unit_price_incl'],
                'articleVat': a['vat_percentage'],
            }
            for a in articles
            if a['unit_price_incl'] > 0
        ]

    @staticmethod
    def _get_gender_from_session():
        """Read and consume the Klarna gender from the HTTP session.

        Returns the gender as int (1=He/Him, 2=She/Her). Klarna won't accept
        the request without it.
        """
        from odoo.http import request as http_request  # noqa: PLC0415
        if not http_request:
            raise ValidationError(
                _("Please select your gender to proceed with Klarna.")
            )
        try:
            gender = http_request.session.pop('buckaroo_klarna_gender', None)
        except RuntimeError:
            gender = None
        if not gender:
            raise ValidationError(
                _("Please select your gender to proceed with Klarna.")
            )
        try:
            return int(gender)
        except (TypeError, ValueError):
            raise ValidationError(
                _("Please select your gender to proceed with Klarna.")
            )

    def _buckaroo_get_payment_action(self):
        """Klarna MOR is always Reserve→Pay; never immediate capture."""
        self.ensure_one()
        if self.code == 'klarna':
            return 'authorize'
        return super()._buckaroo_get_payment_action()

    def _buckaroo_create_payment(self, transaction, client):
        """Klarna flow: add article and customer data, then .reserve()."""
        if self.code != 'klarna':
            return super()._buckaroo_create_payment(transaction, client)

        params = self._buckaroo_get_payment_params(transaction)
        builder = PaymentService(client).create_payment(
            self._buckaroo_get_sdk_service_name(), params,
        )

        # Articles
        articles = get_order_articles(transaction)
        if articles:
            builder.add_parameter('article', self._format_klarna_articles(articles))

        # Top-level Klarna service params
        billing_partner = get_billing_partner(transaction)
        shipping_partner = get_shipping_partner(transaction)
        billing_data = get_customer_data(billing_partner)

        builder.add_parameter('gender', self._get_gender_from_session())
        if billing_data['country_code']:
            builder.add_parameter('operatingCountry', billing_data['country_code'])

        same_address = shipping_partner == billing_partner
        builder.add_parameter('shippingSameAsBilling', 'true' if same_address else 'false')

        # Billing & shipping address (flat ``Billing*`` / ``Shipping*`` params)
        self._add_klarna_address(builder, 'Billing', billing_data)
        shipping_data = billing_data if same_address else get_customer_data(shipping_partner)
        self._add_klarna_address(builder, 'Shipping', shipping_data)

        return builder.reserve(validate=False)

    def _buckaroo_create_capture(self, transaction, client):
        """Klarna capture = Pay action with DataRequestKey from prior Reserve."""
        if self.code != 'klarna':
            return super()._buckaroo_create_capture(transaction, client)
        params, data_request_key = self._buckaroo_get_post_authorize_params(transaction)
        builder = PaymentService(client).create_payment(
            self._buckaroo_get_sdk_service_name(), params,
        )
        builder.add_parameter('dataRequestKey', data_request_key)
        return self._buckaroo_klarna_normalize_idempotent(builder.pay(), 'Captured')

    def _buckaroo_create_void(self, transaction, client):
        """Klarna void = CancelReservation with OriginalTransactionKey."""
        if self.code != 'klarna':
            return super()._buckaroo_create_void(transaction, client)
        params, original_key = self._buckaroo_get_post_authorize_params(transaction)
        builder = PaymentService(client).create_payment(
            self._buckaroo_get_sdk_service_name(), params,
        )
        response = builder.cancelReservation(original_transaction_key=original_key)
        return self._buckaroo_klarna_normalize_idempotent(response, ('Cancelled', 'Canceled'))

    @staticmethod
    def _add_klarna_address(builder, prefix, data):
        """Send a Klarna address group as flat ``{prefix}*`` service params."""
        number, suffix = PaymentMethodKlarna._split_house_number(data['house_number'])
        builder.add_parameter(prefix + 'Street', data['street_name'])
        builder.add_parameter(prefix + 'HouseNumber', number)
        builder.add_parameter(prefix + 'HouseNumberSuffix', suffix)
        builder.add_parameter(prefix + 'PostalCode', data['postal_code'])
        builder.add_parameter(prefix + 'City', data['city'])
        builder.add_parameter(prefix + 'Country', data['country_code'])
        builder.add_parameter(
            prefix + 'CellPhoneNumber', PaymentMethodKlarna._sanitize_phone(data['phone']),
        )
        builder.add_parameter(prefix + 'Email', data['email'])

    @staticmethod
    def _sanitize_phone(value):
        """Return digits-only phone (no '+', no spaces). Klarna's
        ``BillingCellPhoneNumber`` expects pure digits, e.g. ``310612345678``."""
        if not value:
            return ''
        return re.sub(r'\D', '', str(value))

    @staticmethod
    def _sanitize_text(value):
        """Collapse whitespace runs (incl. newlines) into single spaces."""
        if not value:
            return ''
        return re.sub(r'\s+', ' ', str(value)).strip()

    @staticmethod
    def _split_house_number(raw):
        """Split a raw house-number string into ``(number, suffix)``.

        ``"1A"`` → ``("1", "A")``; ``"42"`` → ``("42", "")``.
        Falls back to ``(raw, "")`` when the leading-digits pattern doesn't match.
        """
        if not raw:
            return ('', '')
        match = re.match(r'^\s*(\d+)\s*([A-Za-z][\w\-/]*)?\s*$', str(raw))
        if not match:
            return (str(raw).strip(), '')
        return (match.group(1), match.group(2) or '')

    @staticmethod
    def _buckaroo_klarna_normalize_idempotent(response, target_statuses):
        """Treat Klarna's "order is already in target state" 490 as success.

        Why: Odoo's :func:`service.model.retrying` re-fires the request handler
        on serialization failure, which re-issues the SDK call. The first call
        already moved Klarna's order to a target state; the retry then trips
        ``OrderService_*_InvalidOrderStatus`` and we'd wrongly mark the tx as
        ``error`` despite the operation having succeeded at Klarna.

        ``target_statuses`` is a string or tuple of acceptable Klarna-side
        states (e.g. ``'Captured'`` for capture, ``('Cancelled', 'Canceled')``
        for void — Klarna's casing varies across regions).
        """
        if isinstance(target_statuses, str):
            target_statuses = (target_statuses,)
        if not response.status or not response.status.code:
            return response
        if response.status.code.code != 490:
            return response
        sub = response.status.sub_code
        message = (sub.description if sub else '') or ''
        if 'InvalidOrderStatus' not in message:
            return response
        matched = next(
            (s for s in target_statuses if 'Current status: %s' % s in message),
            None,
        )
        if not matched:
            return response
        response.status.code.code = 190
        response.status.code.description = 'Idempotent: already %s' % matched
        return response


class ResPartner(models.Model):
    _inherit = 'res.partner'

    buckaroo_klarna_gender = fields.Selection(
        selection=[('1', 'He/Him'), ('2', 'She/Her')],
        string="Klarna Salutation",
        help="Saved from the last Klarna checkout to prefill on next order.",
    )
