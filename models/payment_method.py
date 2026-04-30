# Part of Odoo. See LICENSE file for full copyright and licensing details.

import re

from buckaroo.services.payment_service import PaymentService

from odoo import _, api, fields, models
from odoo.addons.payment import utils as payment_utils
from odoo.exceptions import ValidationError
from odoo.tools.misc import format_amount

from ..utils import const


class PaymentMethod(models.Model):
    _inherit = 'payment.method'

    buckaroo_official_is_linked = fields.Boolean(
        compute='_compute_buckaroo_official_is_linked',
        store=True,
    )

    buckaroo_official_sdk_service_name = fields.Char(
        string="SDK Service Name",
        help="PascalCase service name used by the Buckaroo SDK (e.g. 'Visa', 'Mastercard', 'AMEX').",
    )

    buckaroo_official_min_amount = fields.Float(
        string="Buckaroo Min Amount (EUR)",
        digits=(16, 2),
        help=(
            "Minimum order total in EUR for which this payment method is available with "
            "Buckaroo Official. Leave at 0.00 to disable the minimum limit."
        ),
    )
    buckaroo_official_max_amount = fields.Float(
        string="Buckaroo Max Amount (EUR)",
        digits=(16, 2),
        help=(
            "Maximum order total in EUR for which this payment method is available with "
            "Buckaroo Official. Leave at 0.00 to disable the maximum limit."
        ),
    )

    buckaroo_official_fee_amount = fields.Char(
        string="Payment fee",
        default='',
        help=(
            "Per-method payment fee added to the order. Amount is in the order "
            "currency; no FX conversion is performed. Use a fixed amount like "
            "'1.50' or a percentage like '1%'. Leave empty or set to '0' for "
            "no fee."
        ),
    )

    def _is_linked_to_buckaroo(self, provider_ids=None):
        self.ensure_one()
        providers = self.provider_ids
        if provider_ids is not None:
            providers = providers.filtered(lambda p: p.id in provider_ids)
        return any(p.code == const.PROVIDER_CODE for p in providers)

    @api.depends('provider_ids.code')
    def _compute_buckaroo_official_is_linked(self):
        for pm in self:
            pm.buckaroo_official_is_linked = pm._is_linked_to_buckaroo()

    @api.constrains('buckaroo_official_min_amount', 'buckaroo_official_max_amount')
    def _check_buckaroo_official_amount_limits(self):
        for payment_method in self:
            if payment_method.buckaroo_official_min_amount < 0:
                raise ValidationError(_("The Buckaroo minimum amount cannot be negative."))
            if payment_method.buckaroo_official_max_amount < 0:
                raise ValidationError(_("The Buckaroo maximum amount cannot be negative."))
            if (
                payment_method.buckaroo_official_min_amount > 0
                and payment_method.buckaroo_official_max_amount > 0
                and payment_method.buckaroo_official_max_amount
                < payment_method.buckaroo_official_min_amount
            ):
                raise ValidationError(
                    _("The Buckaroo maximum amount must be greater than the minimum amount.")
                )

    @api.constrains('buckaroo_official_fee_amount')
    def _check_buckaroo_official_fee_amount(self):
        for payment_method in self:
            value = (payment_method.buckaroo_official_fee_amount or '').strip()
            if value == '':
                continue
            if not re.match(r'^\d+(?:\.\d+)?%?$', value):
                raise ValidationError(_(
                    "The Buckaroo surcharge must be a positive number "
                    "(e.g. '1.50') or a percentage (e.g. '1%'). "
                    "Leave empty or use '0' for no surcharge."
                ))

    def _buckaroo_official_is_amount_compatible(self, amount, provider_id_set):
        self.ensure_one()
        if not self._is_linked_to_buckaroo(provider_id_set):
            return True
        if self.buckaroo_official_min_amount > 0 and amount < self.buckaroo_official_min_amount:
            return False
        if self.buckaroo_official_max_amount > 0 and amount > self.buckaroo_official_max_amount:
            return False
        return True

    def _get_compatible_payment_methods(
        self, provider_ids, partner_id, currency_id=None, force_tokenization=False,
        is_express_checkout=False, report=None, **kwargs
    ):
        payment_methods = super()._get_compatible_payment_methods(
            provider_ids, partner_id, currency_id=currency_id,
            force_tokenization=force_tokenization,
            is_express_checkout=is_express_checkout, report=report, **kwargs
        )
        amount = kwargs.get('amount')
        if amount is None and kwargs.get('sale_order_id'):
            order = self.env['sale.order'].sudo().browse(kwargs['sale_order_id']).exists()
            amount = order.amount_total if order else None
        if amount is None:
            return payment_methods

        provider_id_set = set(provider_ids)
        payment_methods.mapped('provider_ids.code')  # batch prefetch
        unfiltered_payment_methods = payment_methods
        payment_methods = payment_methods.filtered(
            lambda pm: pm._buckaroo_official_is_amount_compatible(amount, provider_id_set)
        )
        payment_utils.add_to_report(
            report, unfiltered_payment_methods - payment_methods,
            available=False,
            reason=_("Amount outside the Buckaroo payment method limits."),
        )

        return payment_methods


    def _buckaroo_parse_fee_amount(self):
        self.ensure_one()
        raw = (self.buckaroo_official_fee_amount or '').strip()
        if not raw:
            return False, 0.0
        is_percent = raw.endswith('%')
        value = float(raw[:-1] or '0') if is_percent else float(raw)
        return is_percent, value

    def _buckaroo_get_label_suffix(self, currency):
        self.ensure_one()
        is_percent, value = self._buckaroo_parse_fee_amount()
        if value == 0.0:
            return ''
        if is_percent:
            return f' (+ {self.buckaroo_official_fee_amount.strip()[:-1]}%)'
        if not currency:
            return ''
        return f' (+ {format_amount(self.env, value, currency)})'

    def _buckaroo_compute_surcharge_amount(self, subtotal, currency):
        """Compute the surcharge for *subtotal* in *currency*, rounded."""
        self.ensure_one()
        is_percent, value = self._buckaroo_parse_fee_amount()
        amount = subtotal * value / 100.0 if is_percent else value
        return currency.round(amount)

    def _buckaroo_get_surcharge_line_name(self):
        """Return the order-line label for this method's surcharge."""
        self.ensure_one()
        return _('%s surcharge', self.name)

    def _buckaroo_get_sdk_service_name(self):
        """Return the SDK service name for this payment method.

        Uses the ``buckaroo_official_sdk_service_name`` field when set,
        otherwise falls back to ``self.code``.
        """
        self.ensure_one()
        return self.buckaroo_official_sdk_service_name or self.code

    @staticmethod
    def _buckaroo_resolve_description(template, transaction):
        """Replace placeholders in a description template.

        Supported placeholders: {order_number}, {shop_name}.
        Returns the transaction reference when *template* is empty.
        """
        if not template:
            return transaction.reference

        label = template.replace('{order_number}', transaction.reference or '')
        label = label.replace(
            '{shop_name}',
            transaction.company_id.name if transaction.company_id else '',
        )

        # Strip any unsupported placeholders (e.g. from old templates).
        label = re.sub(r'\{[a-z_]+\}', '', label).strip()

        return label or transaction.reference

    def _buckaroo_get_payment_action(self):
        """Return the payment action for this method: 'pay', 'authorize', or None.

        Base returns ``None`` (default 'pay' flow). Subclasses override to
        return 'authorize' when their configuration selects it.
        """
        self.ensure_one()
        return None

    def _buckaroo_get_payment_params(self, transaction, description_template=None):
        """Build the dict passed to ``PaymentService.create_payment().from_dict()``.

        Override in subclasses (via method-specific logic) to inject extra
        parameters.  Call ``super()`` and merge when you only need to *extend*
        the defaults.
        """
        self.ensure_one()
        provider = transaction.provider_id
        base_url = provider.get_base_url().rstrip('/')
        return_url = f"{base_url}/payment/buckaroo_official/return"
        webhook_url = f"{base_url}/payment/buckaroo_official/webhook"
        if description_template is None:
            description_template = provider.buckaroo_official_transaction_description
        description = self._buckaroo_resolve_description(description_template, transaction)
        return {
            'currency': transaction.currency_id.name,
            'amount': transaction.amount,
            'description': description,
            'invoice': transaction.reference,
            'return_url': return_url,
            'return_url_cancel': return_url,
            'return_url_error': return_url,
            'return_url_reject': return_url,
            'push_url': webhook_url,
            'push_url_failure': webhook_url,
        }


    def _buckaroo_create_payment(self, transaction, client):
        """Create a payment via the SDK and return the ``PaymentResponse``.

        Default flow: build params, create_payment(...).pay(). Subclasses
        override for method-specific dispatch.
        """
        self.ensure_one()
        params = self._buckaroo_get_payment_params(transaction)
        return PaymentService(client).create_payment(
            self._buckaroo_get_sdk_service_name(), params,
        ).pay()


    def _buckaroo_extract_redirect_url(self, response):
        """Return the redirect URL from the SDK *response*."""
        self.ensure_one()
        redirect_url = response.get_redirect_url()
        if not redirect_url and response.required_action:
            redirect_url = response.required_action.redirect_url
        return redirect_url


    def _buckaroo_get_refund_params(self, source_tx, refund_tx):
        """Build the dict passed to the SDK for a refund request."""
        self.ensure_one()
        provider = refund_tx.provider_id
        template = (provider.buckaroo_official_refund_description
                    or provider.buckaroo_official_transaction_description)
        params = self._buckaroo_get_payment_params(refund_tx, description_template=template)
        params['original_transaction_key'] = source_tx.provider_reference
        params['refund_amount'] = abs(refund_tx.amount)
        return params

    def _buckaroo_create_refund(self, source_tx, refund_tx, client):
        """Execute a refund via the SDK and return the ``PaymentResponse``."""
        self.ensure_one()
        params = self._buckaroo_get_refund_params(source_tx, refund_tx)
        return PaymentService(client).create_payment(
            self._buckaroo_get_sdk_service_name(), params,
        ).refund()


    def _buckaroo_get_post_authorize_params(self, transaction):
        """Build the params dict and original transaction key for capture/void.

        :returns: ``(params, original_transaction_key)`` tuple.
        """
        self.ensure_one()
        provider = transaction.provider_id
        template = provider.buckaroo_official_transaction_description
        params = self._buckaroo_get_payment_params(transaction, description_template=template)
        source_tx = transaction.source_transaction_id or transaction
        original_transaction_key = source_tx.provider_reference
        return params, original_transaction_key

    def _buckaroo_execute_post_authorize(self, transaction, client, sdk_method):
        """Shared builder for capture and void SDK calls."""
        self.ensure_one()
        params, original_key = self._buckaroo_get_post_authorize_params(transaction)
        builder = PaymentService(client).create_payment(
            self._buckaroo_get_sdk_service_name(), params,
        )
        return getattr(builder, sdk_method)(original_transaction_key=original_key)

    def _buckaroo_create_capture(self, transaction, client):
        """Execute a capture via the SDK."""
        return self._buckaroo_execute_post_authorize(transaction, client, 'capture')

    def _buckaroo_create_void(self, transaction, client):
        """Execute a cancel-authorize via the SDK."""
        return self._buckaroo_execute_post_authorize(transaction, client, 'cancelAuthorize')
