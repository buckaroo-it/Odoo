# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
import re

from buckaroo.services.payment_service import PaymentService

from odoo import _, api, fields, models
from odoo.addons.payment import utils as payment_utils
from odoo.exceptions import ValidationError
from odoo.tools.misc import format_amount

from ..utils import const

_logger = logging.getLogger(__name__)


class PaymentMethod(models.Model):
    _inherit = "payment.method"

    buckaroo_official_is_linked = fields.Boolean(
        compute="_compute_buckaroo_official_is_linked",
        store=True,
    )

    buckaroo_official_sdk_service_name = fields.Char(
        string="SDK Service Name",
        help="Service name used by the Buckaroo SDK (e.g. 'ideal', 'Visa', 'transfer', 'giftcards').",
    )

    buckaroo_official_min_amount = fields.Char(
        string="Buckaroo Min Amount (EUR)",
        default="",
        help=(
            "Minimum order total in EUR for which this payment method is available with "
            "Buckaroo Official. Leave empty to disable the minimum limit."
        ),
    )
    buckaroo_official_max_amount = fields.Char(
        string="Buckaroo Max Amount (EUR)",
        default="",
        help=(
            "Maximum order total in EUR for which this payment method is available with "
            "Buckaroo Official. Leave empty to disable the maximum limit."
        ),
    )

    buckaroo_official_fee_amount = fields.Char(
        string="Payment fee",
        default="",
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

    @api.depends("provider_ids.code")
    def _compute_buckaroo_official_is_linked(self):
        for pm in self:
            pm.buckaroo_official_is_linked = pm._is_linked_to_buckaroo()

    @api.constrains("buckaroo_official_min_amount", "buckaroo_official_max_amount")
    def _check_buckaroo_official_amount_limits(self):
        for pm in self:
            min_v = pm._buckaroo_parse_amount_limit(pm.buckaroo_official_min_amount)
            max_v = pm._buckaroo_parse_amount_limit(pm.buckaroo_official_max_amount)
            if min_v is not None and max_v is not None and max_v < min_v:
                raise ValidationError(
                    _("The Buckaroo maximum amount must be greater than the minimum amount.")
                )

    @staticmethod
    def _buckaroo_parse_amount_limit(value):
        """Empty → ``None`` (no limit); non-numeric → ValidationError."""
        raw = (value or "").strip()
        if not raw:
            return None
        if not re.match(r"^\d+(?:\.\d+)?$", raw):
            raise ValidationError(
                _(
                    "The Buckaroo amount limit must be a positive number "
                    "(e.g. '100.00'). Leave empty for no limit."
                )
            )
        return float(raw)

    @api.constrains("buckaroo_official_fee_amount")
    def _check_buckaroo_official_fee_amount(self):
        for payment_method in self:
            value = (payment_method.buckaroo_official_fee_amount or "").strip()
            if value == "":
                continue
            if not re.match(r"^\d+(?:\.\d+)?%?$", value):
                raise ValidationError(
                    _(
                        "The Buckaroo surcharge must be a positive number "
                        "(e.g. '1.50') or a percentage (e.g. '1%'). "
                        "Leave empty or use '0' for no surcharge."
                    )
                )

    def _buckaroo_official_is_amount_compatible(self, amount, provider_id_set):
        self.ensure_one()
        if not self._is_linked_to_buckaroo(provider_id_set):
            return True
        min_v = self._buckaroo_parse_amount_limit(self.buckaroo_official_min_amount)
        max_v = self._buckaroo_parse_amount_limit(self.buckaroo_official_max_amount)
        if min_v is not None and amount < min_v:
            return False
        if max_v is not None and amount > max_v:
            return False
        return True

    def _get_compatible_payment_methods(
        self,
        provider_ids,
        partner_id,
        currency_id=None,
        force_tokenization=False,
        is_express_checkout=False,
        report=None,
        **kwargs,
    ):
        payment_methods = super()._get_compatible_payment_methods(
            provider_ids,
            partner_id,
            currency_id=currency_id,
            force_tokenization=force_tokenization,
            is_express_checkout=is_express_checkout,
            report=report,
            **kwargs,
        )
        amount = kwargs.get("amount")
        if amount is None and kwargs.get("sale_order_id"):
            order = self.env["sale.order"].sudo().browse(kwargs["sale_order_id"]).exists()
            if order:
                amount = order.amount_total
        if amount is None:
            return payment_methods

        provider_id_set = set(provider_ids)
        # Batch prefetch provider codes before filtering one-by-one.
        payment_methods.mapped("provider_ids.code")
        unfiltered_payment_methods = payment_methods
        payment_methods = payment_methods.filtered(
            lambda pm: pm._buckaroo_official_is_amount_compatible(amount, provider_id_set)
        )
        payment_utils.add_to_report(
            report,
            unfiltered_payment_methods - payment_methods,
            available=False,
            reason=_("Amount outside the Buckaroo payment method limits."),
        )

        return payment_methods

    def _buckaroo_parse_fee_amount(self):
        self.ensure_one()
        raw = (self.buckaroo_official_fee_amount or "").strip()
        if not raw:
            return False, 0.0
        is_percent = raw.endswith("%")
        value = float(raw[:-1] or "0") if is_percent else float(raw)
        return is_percent, value

    def _buckaroo_get_label_suffix(self, currency):
        self.ensure_one()
        is_percent, value = self._buckaroo_parse_fee_amount()
        if value == 0.0:
            return ""
        if is_percent:
            return f" (+ {self.buckaroo_official_fee_amount.strip()[:-1]}%)"
        if not currency:
            return ""
        return f" (+ {format_amount(self.env, value, currency)})"

    def _buckaroo_compute_surcharge_amount(self, subtotal, currency):
        self.ensure_one()
        is_percent, value = self._buckaroo_parse_fee_amount()
        amount = subtotal * value / 100.0 if is_percent else value
        return currency.round(amount)

    def _buckaroo_get_surcharge_line_name(self):
        self.ensure_one()
        return _("%s surcharge", self.name)

    @staticmethod
    def _buckaroo_resolve_description(template, transaction):
        """Substitute ``{order_number}`` and ``{shop_name}`` in *template*.

        Returns the transaction reference when *template* is empty.
        """
        if not template:
            return transaction.reference

        label = template.replace("{order_number}", transaction.reference or "")
        label = label.replace(
            "{shop_name}",
            transaction.company_id.name if transaction.company_id else "",
        )

        # Strip unsupported placeholders left over from old templates.
        label = re.sub(r"\{[a-z_]+\}", "", label).strip()

        return label or transaction.reference

    def _buckaroo_get_payment_action(self):
        """``'pay'``, ``'authorize'``, or ``None`` (default ``'pay'``)."""
        self.ensure_one()
        return None

    def _buckaroo_get_payment_params(self, transaction, description_template=None):
        """Build the dict passed to ``PaymentService.create_payment().from_dict()``."""
        self.ensure_one()
        provider = transaction.provider_id
        base_url = provider.get_base_url().rstrip("/")
        return_url = f"{base_url}/payment/buckaroo_official/return"
        webhook_url = f"{base_url}/payment/buckaroo_official/webhook"
        if description_template is None:
            description_template = provider.buckaroo_official_transaction_description
        description = self._buckaroo_resolve_description(description_template, transaction)
        return {
            "currency": transaction.currency_id.name,
            "amount": transaction.amount,
            "description": description,
            "invoice": transaction.reference,
            "return_url": return_url,
            "return_url_cancel": return_url,
            "return_url_error": return_url,
            "return_url_reject": return_url,
            "push_url": webhook_url,
            "push_url_failure": webhook_url,
        }

    def _buckaroo_create_payment(self, transaction, client):
        self.ensure_one()
        params = self._buckaroo_get_payment_params(transaction)
        builder = PaymentService(client).create_payment(
            self.buckaroo_official_sdk_service_name,
            params,
        )
        return self._buckaroo_submit_payment(builder, transaction)

    def _buckaroo_submit_payment(self, builder, transaction):
        """Submit the built payment request. Hook for methods that must use an
        action other than a plain Pay (see the giftcard override, which routes a
        remainder leg through PayRemainder)."""
        self.ensure_one()
        return builder.pay()

    def _buckaroo_extract_redirect_url(self, response):
        self.ensure_one()
        redirect_url = response.get_redirect_url()
        if not redirect_url and response.required_action:
            redirect_url = response.required_action.redirect_url
        return redirect_url

    def _buckaroo_handle_no_redirect_response(self, transaction, response):
        """Hook for methods that legitimately return no external redirect
        (e.g. Bank Transfer's merchant-display mode). Return ``None`` to
        fall through to the default error path, or a dict like
        ``{'api_url': '...'}`` to short-circuit ``_get_specific_processing_values``.

        A redirect method can also settle inline: the gateway completes the
        payment on the Pay call and returns SUCCESS (190) with no
        RequiredAction and no redirect URL (e.g. EPS in test mode). Without
        this, the caller reads that as "no redirect URL" and raises the
        response's status message ("Transaction successfully processed") as an
        error, rolling back the whole request so no transaction or order is
        created. Settle a terminal success instead: record the key, move the
        tx to its terminal state, and route to ``/payment/status`` so its poll
        post-processes the tx in a fresh request (see the PayPal override for
        why not ``/shop/payment/validate``). Return ``None`` for any
        non-success response so the caller falls through to its error
        handling.

        The inline path intentionally skips ``_validate_amount`` (which only
        runs on the push/return ``_process`` path): the amount is echoed from
        the create request we just sent, so it is trusted here — consistent
        with the PayPal and Riverty inline-settlement hooks."""
        self.ensure_one()
        status_code = (
            response.status.code.code if response.status and response.status.code else None
        )
        if status_code != const.BuckarooStatusCode.SUCCESS:
            return None
        transaction.provider_reference = response.key
        if transaction.buckaroo_official_payment_action == "authorize":
            transaction._set_authorized()
        else:
            transaction._set_done()
        base_url = transaction.provider_id.get_base_url().rstrip("/")
        return {"api_url": f"{base_url}/payment/status"}

    def _buckaroo_handle_redirect_response(self, transaction, response):
        self.ensure_one()
        return None

    def _buckaroo_apply_push_metadata(self, transaction, payment_data):
        self.ensure_one()
        return

    def _buckaroo_extract_amount_data(self, transaction, payment_data):
        self.ensure_one()
        credit_amount = payment_data.credit_amount
        amount = payment_data.amount or (abs(credit_amount) if credit_amount else None)
        currency = payment_data.currency
        if amount is None or not currency:
            return None
        return {
            "amount": amount,
            "currency_code": currency,
        }

    def _buckaroo_apply_push_identity(self, transaction, payment_data):
        self.ensure_one()
        txn_key = payment_data.transaction_key
        if txn_key:
            transaction.provider_reference = txn_key
        service_code = payment_data.service_code
        if service_code:
            transaction.buckaroo_official_service_code = service_code

    def _buckaroo_handle_duplicate_push(self, transaction, payment_data):
        """Spawn an ``R-`` child when Buckaroo emits a refund push for an
        already-done tx (Plaza-initiated refunds for any method). Otherwise
        log + skip the duplicate."""
        self.ensure_one()
        if payment_data.is_success():
            refund_relation = payment_data.get_refund_relation()
            if refund_relation:
                target = self._buckaroo_resolve_refund_target(transaction, refund_relation)
                if target:
                    self._buckaroo_spawn_refund_child(target, payment_data)
                    return
        _logger.info(
            "Skipping duplicate Buckaroo callback for transaction %s (state=%s)",
            transaction.reference,
            transaction.state,
        )

    @staticmethod
    def _buckaroo_resolve_refund_target(transaction, refund_relation):
        """Locate the tx whose ``provider_reference`` matches the push's
        ``RelatedTransactions.Refund`` key. The matched-by-invoice tx may
        point at the wrong row (e.g. the giftcard partial leg when the
        refund actually targets its GCR child)."""
        if transaction.provider_reference == refund_relation:
            return transaction
        return transaction.search(
            [
                ("provider_id", "=", transaction.provider_id.id),
                ("provider_reference", "=", refund_relation),
            ],
            limit=1,
        )

    @staticmethod
    def _buckaroo_spawn_refund_child(transaction, payment_data):
        """Create the ``R-`` child for a Plaza-initiated refund push."""
        txn_key = payment_data.transaction_key
        currency = transaction.currency_id
        amount = payment_data.credit_amount or payment_data.amount or 0.0
        if currency.compare_amounts(amount, 0) <= 0:
            _logger.info(
                "Buckaroo refund push for %s carried no positive amount; skipping",
                transaction.reference,
            )
            return transaction.browse()
        if txn_key:
            existing = transaction.search(
                [
                    ("provider_id", "=", transaction.provider_id.id),
                    ("provider_reference", "=", txn_key),
                ],
                limit=1,
            )
            if existing:
                return existing
        refund_tx = transaction._create_child_transaction(
            amount,
            is_refund=True,
            provider_reference=txn_key,
            buckaroo_official_service_code=(
                payment_data.service_code or transaction.buckaroo_official_service_code
            ),
        )
        refund_tx._set_done()
        refund_tx._post_process()
        _logger.info(
            "Spawned Plaza-initiated refund child %s for %s (amount %s)",
            refund_tx.reference,
            transaction.reference,
            amount,
        )
        return refund_tx

    def _buckaroo_adjust_amount_on_success(self, transaction, payment_data):
        self.ensure_one()
        return

    def _buckaroo_split_remainder_push(self, transaction, payment_data):
        self.ensure_one()
        capture_child = self._buckaroo_spawn_capture_from_push(transaction, payment_data)
        if capture_child:
            return capture_child
        return transaction.browse()

    def _buckaroo_spawn_capture_from_push(self, transaction, payment_data):
        """Spawn a capture child when a 190 push lands on an authorized tx with
        a transaction key that differs from the auth's provider_reference.

        Plaza and other external captures arrive as a Collecting push on the
        original auth tx; without a child the auth alone can't hold both
        states (authorized + done) and the W8 refund-key resolver has nowhere
        to read the capture key from. Routes the push to the new child so
        ``_apply_updates`` sets it to done; the framework's
        ``_update_source_transaction_state`` then promotes the parent.
        """
        self.ensure_one()
        if transaction.buckaroo_official_payment_action != "authorize":
            return None
        if transaction.state != "authorized":
            return None
        if not payment_data.is_success():
            return None
        push_key = payment_data.transaction_key
        if not push_key or push_key == transaction.provider_reference:
            return None
        # Skip on redelivery and when the admin-button path already spawned
        # a capture child (operation copies parent's, so filter out refunds).
        # A concurrent second Plaza push racing this check is serialized by the
        # ``FOR UPDATE NOWAIT`` lock in the controller's ``_handle_push``: the
        # loser retries with a fresh snapshot and sees this child. An admin
        # Capture races through a separate flow and is not covered here.
        if transaction.child_transaction_ids.filtered(lambda t: t.operation != "refund"):
            return None
        child_amount = payment_data.amount or transaction.amount
        return transaction._create_child_transaction(child_amount)

    def _buckaroo_failure_message_hint(self, transaction, response, operation, status_code):
        self.ensure_one()
        return None

    def _buckaroo_require_service_code(self, source_tx, missing_message):
        self.ensure_one()
        tx = source_tx
        service_code = tx.buckaroo_official_service_code
        seen = {tx.id}
        while (
            not service_code
            and tx.source_transaction_id
            and tx.source_transaction_id.id not in seen
        ):
            tx = tx.source_transaction_id
            seen.add(tx.id)
            service_code = tx.buckaroo_official_service_code
        if not service_code:
            raise ValidationError(missing_message)
        return service_code

    def _buckaroo_resolve_original_transaction_key(self, source_tx):
        """Pick the provider_reference to send as Buckaroo's
        ``OriginalTransactionKey`` when refunding ``source_tx``. When
        ``source_tx`` was authorized and then captured, the gateway
        wants the capture's key — sending the auth key trips a 490
        "Invalid parameter: originaltransaction".
        """
        self.ensure_one()
        # "Latest wins" assumes a single full capture (support_manual_capture is
        # full_only). Mixed Odoo+Plaza partial captures are out of scope.
        capture_child = source_tx.child_transaction_ids.filtered(
            lambda t: t.state == "done" and t.operation != "refund" and t.provider_reference
        ).sorted("id")[-1:]
        if capture_child:
            return capture_child.provider_reference
        return source_tx.provider_reference

    def _buckaroo_get_refund_params(self, source_tx, refund_tx):
        self.ensure_one()
        provider = refund_tx.provider_id
        template = (
            provider.buckaroo_official_refund_description
            or provider.buckaroo_official_transaction_description
        )
        params = self._buckaroo_get_payment_params(refund_tx, description_template=template)
        params["original_transaction_key"] = self._buckaroo_resolve_original_transaction_key(
            source_tx
        )
        params["refund_amount"] = abs(refund_tx.amount)
        return params

    def _buckaroo_create_refund(self, source_tx, refund_tx, client):
        self.ensure_one()
        params = self._buckaroo_get_refund_params(source_tx, refund_tx)
        return (
            PaymentService(client)
            .create_payment(
                self.buckaroo_official_sdk_service_name,
                params,
            )
            .refund()
        )

    def _buckaroo_get_post_authorize_params(self, transaction):
        """Returns ``(params, original_transaction_key)`` for capture/void."""
        self.ensure_one()
        provider = transaction.provider_id
        template = provider.buckaroo_official_transaction_description
        params = self._buckaroo_get_payment_params(transaction, description_template=template)
        source_tx = transaction.source_transaction_id or transaction
        original_transaction_key = source_tx.provider_reference
        return params, original_transaction_key

    def _buckaroo_create_capture(self, transaction, client):
        self.ensure_one()
        params, original_key = self._buckaroo_get_post_authorize_params(transaction)
        builder = PaymentService(client).create_payment(
            self.buckaroo_official_sdk_service_name,
            params,
        )
        return builder.capture(original_transaction_key=original_key)

    def _buckaroo_create_void(self, transaction, client):
        self.ensure_one()
        params, original_key = self._buckaroo_get_post_authorize_params(transaction)
        builder = PaymentService(client).create_payment(
            self.buckaroo_official_sdk_service_name,
            params,
        )
        return builder.cancelAuthorize(original_transaction_key=original_key)
