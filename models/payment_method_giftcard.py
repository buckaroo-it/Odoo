# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

from psycopg2.errors import UniqueViolation

from buckaroo.services.payment_service import PaymentService

from odoo import _, fields, models
from odoo.addons.payment import utils as payment_utils
from odoo.addons.website_sale.models.website import CART_SESSION_CACHE_KEY
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Command
from odoo.http import request

from ..utils import const

_logger = logging.getLogger(__name__)


class PaymentMethodGiftcard(models.Model):
    _inherit = "payment.method"

    buckaroo_official_giftcard_method = fields.Selection(
        string="Giftcard Method",
        selection=[("redirect", "Redirect"), ("inline", "Inline")],
        default="redirect",
        help="Choose how giftcard payments are handled. "
        "'Redirect' sends the customer to Buckaroo's hosted page. "
        "'Inline' collects the card number and PIN directly on the checkout page.",
    )
    buckaroo_official_giftcard_backend = fields.Selection(
        string="Giftcard Backend",
        selection=[
            ("intersolve", "Intersolve"),
            ("fashioncheque", "Fashioncheque"),
            ("tcs", "TCS"),
        ],
        help="Backend that processes this giftcard brand. Drives inline "
        "parameter names and refund requirements (Intersolve needs LastName/Email).",
    )
    buckaroo_official_giftcard_cardnumber_param = fields.Char(
        string="Giftcard Card Number Parameter",
        help="Buckaroo SDK parameter name carrying the card number for this brand.",
    )
    buckaroo_official_giftcard_pin_param = fields.Char(
        string="Giftcard PIN Parameter",
        help="Buckaroo SDK parameter name carrying the PIN for this brand.",
    )

    def _is_buckaroo_giftcard(self):
        self.ensure_one()
        return (self.primary_payment_method_id or self).code == "buckaroo_giftcard"

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

        buckaroo_provider_ids = set(
            self.env["payment.provider"]
            .browse(list(provider_ids))
            .filtered(lambda p: p.code == const.PROVIDER_CODE)
            .ids
        )
        if not buckaroo_provider_ids:
            return payment_methods

        parent = self.env.ref(
            "payment_buckaroo_official.payment_method_giftcard",
            raise_if_not_found=False,
        )
        if not parent:
            return payment_methods

        mode = parent.buckaroo_official_giftcard_method or "redirect"
        if mode != "inline":
            return payment_methods

        # Inline mode: hide the parent picker, surface brand sub-methods so
        # each brand renders its own inline form.
        payment_methods = payment_methods - parent
        surfaced = self.env["payment.method"]
        for brand in parent.brand_ids:
            if brand.provider_ids.filtered(lambda p: p.id in buckaroo_provider_ids):
                surfaced |= brand
        surfaced = surfaced.filtered("active")

        amount = kwargs.get("amount")
        if amount is None and kwargs.get("sale_order_id"):
            order = self.env["sale.order"].sudo().browse(kwargs["sale_order_id"]).exists()
            if order:
                amount = order.amount_total
        if amount is not None:
            unfiltered = surfaced
            surfaced = surfaced.filtered(
                lambda pm: pm._buckaroo_official_is_amount_compatible(amount, buckaroo_provider_ids)
            )
            payment_utils.add_to_report(
                report,
                unfiltered - surfaced,
                available=False,
                reason=_("Amount outside the Buckaroo payment method limits."),
            )

        return payment_methods | surfaced

    def _buckaroo_giftcard_primary(self):
        """Giftcard parent for *self*; empty recordset for non-giftcard methods."""
        self.ensure_one()
        if not self._is_buckaroo_giftcard():
            return self.browse()
        return self.primary_payment_method_id or self

    def _buckaroo_giftcard_param_names(self):
        """``(cardnumber_param_name, pin_param_name)`` for *self* (a brand)."""
        self.ensure_one()
        return (
            self.buckaroo_official_giftcard_cardnumber_param or "Cardnumber",
            self.buckaroo_official_giftcard_pin_param or "PIN",
        )

    @staticmethod
    def _buckaroo_giftcard_selectable_services(primary, provider):
        """Comma-joined SDK service codes the gateway picker offers: brand
        sub-methods first, then other active top-level methods on the provider."""
        brand_codes = [
            m.buckaroo_official_sdk_service_name for m in primary.brand_ids.filtered("active")
        ]
        other_codes = [
            m.buckaroo_official_sdk_service_name
            for m in provider.payment_method_ids.filtered("active")
            .filtered(lambda m: not m.primary_payment_method_id)
            .filtered(lambda m: m.id != primary.id)
        ]
        return ",".join(dict.fromkeys(c for c in brand_codes + other_codes if c))

    def _buckaroo_create_payment(self, transaction, client):
        self.ensure_one()
        primary = self._buckaroo_giftcard_primary()
        if not primary:
            return super()._buckaroo_create_payment(transaction, client)

        params = self._buckaroo_get_payment_params(transaction)
        params["continue_on_incomplete"] = "1"

        if not self.primary_payment_method_id:
            params["services_selectable_by_client"] = self._buckaroo_giftcard_selectable_services(
                primary,
                transaction.provider_id,
            )
            return PaymentService(client).create_payment("giftcards", params).pay_redirect()

        brand_code = self.buckaroo_official_sdk_service_name
        params["giftcard_name"] = brand_code
        transaction.buckaroo_official_service_code = brand_code

        if (primary.buckaroo_official_giftcard_method or "redirect") != "inline":
            return PaymentService(client).create_payment("giftcards", params).pay()
        return self._buckaroo_create_giftcard_inline(transaction, client, params)

    def _buckaroo_submit_payment(self, builder, transaction):
        group_key = transaction._buckaroo_giftcard_remainder_group_key()
        if group_key:
            # Remainder leg of an inline giftcard partial: settle it into the
            # existing giftcard group instead of opening a standalone payment.
            return builder.pay_remainder(original_transaction_key=group_key)
        return super()._buckaroo_submit_payment(builder, transaction)

    def _buckaroo_create_giftcard_inline(self, transaction, client, params):
        self.ensure_one()
        ctx = transaction.env.context
        cardnumber = ctx.get("buckaroo_giftcard_cardnumber")
        pin = ctx.get("buckaroo_giftcard_pin")
        if not cardnumber or not pin:
            raise ValidationError(
                _("Giftcard details missing. Please re-enter the card number and PIN.")
            )

        builder = PaymentService(client).create_payment("giftcards", params)
        card_name, pin_name = self._buckaroo_giftcard_param_names()
        builder.add_parameter(card_name, cardnumber)
        builder.add_parameter(pin_name, pin)
        return builder.pay()

    def _buckaroo_handle_no_redirect_response(self, transaction, response):
        if not self._is_buckaroo_giftcard():
            return super()._buckaroo_handle_no_redirect_response(transaction, response)

        status_code = (
            response.status.code.code if response.status and response.status.code else None
        )
        if status_code != const.BuckarooStatusCode.SUCCESS:
            return None

        if self._buckaroo_extract_redirect_url(response):
            return None

        transaction.provider_reference = response.key
        transaction._set_done()
        base_url = transaction.provider_id.get_base_url().rstrip("/")
        return {"api_url": f"{base_url}/payment/status"}

    def _buckaroo_handle_redirect_response(self, transaction, response):
        """Inline-mode partial-pay: record the consumed slice, capture the
        group transaction key, then bounce the shopper to ``/shop/payment`` so
        Odoo drives the remainder. The remainder leg is sent as a PayRemainder
        against the captured group key (see ``payment.transaction.
        _buckaroo_giftcard_remainder_group_key``), so Buckaroo settles giftcard
        + remainder as one group transaction without the shopper leaving the
        merchant's checkout."""
        primary = self._buckaroo_giftcard_primary()
        if not primary:
            return super()._buckaroo_handle_redirect_response(transaction, response)
        if (primary.buckaroo_official_giftcard_method or "redirect") != "inline":
            return None
        # Gate on status_code, not is_successful_payment — the SDK reads the
        # FULL order settlement and reports false even when the slice succeeds.
        status_code = (
            response.status.code.code if response.status and response.status.code else None
        )
        if status_code != const.BuckarooStatusCode.SUCCESS:
            return None
        if not (response.required_action and response.required_action.redirect_url):
            return None

        transaction.provider_reference = response.key
        group_key = self._buckaroo_extract_group_transaction_key(response)
        if group_key:
            transaction.sale_order_ids[:1].buckaroo_official_group_transaction_key = group_key
        consumed = self._buckaroo_giftcard_partial_amount(transaction, response)
        if consumed is None:
            transaction._set_pending()
            _logger.info(
                "Buckaroo giftcard partial-pay for %s: consumed amount unknown "
                "from sync response; left tx pending until webhook arrives",
                transaction.reference,
            )
        else:
            transaction.amount = consumed
            transaction._set_done()
            # /shop/payment bounce skips /payment/status, so post-process must
            # fire inline or account_payment never creates the payment record.
            transaction._post_process()
        base_url = transaction.provider_id.get_base_url().rstrip("/")
        return {"api_url": f"{base_url}/shop/payment"}

    @staticmethod
    def _buckaroo_extract_group_transaction_key(response):
        """Group transaction key from a giftcard partial-pay response, used as
        OriginalTransactionKey on the PayRemainder that settles the open amount.
        Buckaroo returns it under ``RequiredAction.PayRemainderDetails.
        GroupTransaction``; fall back to the first related transaction key.

        Called only after the caller has confirmed ``response.required_action``
        is set, so the attribute is accessed directly."""
        details = response.required_action.pay_remainder_details
        if isinstance(details, dict):
            key = details.get("GroupTransaction")
            if key:
                return key
        related = response.related_transactions
        if related and isinstance(related[0], dict):
            return related[0].get("RelatedTransactionKey")
        return None

    @staticmethod
    def _buckaroo_giftcard_partial_amount(transaction, response):
        """Consumed slice on partial-pay; ``None`` when nothing signals partial.

        Preference: AmountDebit service param, then RemainderAmount, then root
        ``amount_debit`` (only when strictly less than tx.amount — root field
        sometimes mirrors the requested amount instead of the drawn slice).
        """
        debit = response.get_service_parameter("AmountDebit")
        if debit is not None:
            try:
                return float(debit)
            except (TypeError, ValueError):
                pass
        remainder = response.get_service_parameter("RemainderAmount")
        if remainder is not None:
            try:
                return float(transaction.amount) - float(remainder)
            except (TypeError, ValueError):
                pass
        root_debit = getattr(response, "amount_debit", None)
        if root_debit is not None:
            try:
                root_value = float(root_debit)
            except (TypeError, ValueError):
                root_value = None
            currency = transaction.currency_id
            if (
                root_value is not None
                and currency.compare_amounts(root_value, 0) > 0
                and currency.compare_amounts(root_value, transaction.amount) < 0
            ):
                return root_value
        return None

    def _buckaroo_get_refund_params(self, source_tx, refund_tx):
        if not self._is_buckaroo_giftcard():
            return super()._buckaroo_get_refund_params(source_tx, refund_tx)
        params = super()._buckaroo_get_refund_params(source_tx, refund_tx)
        brand_code = self._buckaroo_require_giftcard_brand(source_tx)
        params["giftcard_name"] = brand_code
        # In redirect mode source_tx.payment_method_id is the giftcard parent
        # (no backend); resolve the brand sub-method via service code so
        # backend-specific refund params still get injected.
        brand_method = self._buckaroo_resolve_giftcard_brand(brand_code)
        if brand_method.buckaroo_official_giftcard_backend == "intersolve":
            # Plaza rejects Intersolve refunds with status 690 without these.
            target = source_tx.partner_id.commercial_partner_id or source_tx.partner_id
            service_params = params.setdefault("service_parameters", {})
            service_params["LastName"] = self._buckaroo_intersolve_lastname(target)
            email = target.email or source_tx.partner_id.email
            if email:
                service_params["Email"] = email
        return params

    def _buckaroo_resolve_giftcard_brand(self, service_code):
        """Brand sub-method whose SDK service name matches *service_code*."""
        parent = self._buckaroo_giftcard_primary()
        if not parent:
            return self.env["payment.method"]
        return parent.brand_ids.filtered(
            lambda m: m.buckaroo_official_sdk_service_name == service_code
        )[:1]

    @staticmethod
    def _buckaroo_intersolve_lastname(partner):
        """Non-empty LastName for an Intersolve refund."""
        if "lastname" in partner._fields and partner.lastname:
            return partner.lastname
        name = (partner.name or "").strip()
        if not name:
            return "Customer"
        return name.rsplit(" ", 1)[-1] or name

    def _buckaroo_create_refund(self, source_tx, refund_tx, client):
        # Brand codes aren't registered SDK builders; route through "giftcards"
        # so the service entry isn't omitted (gateway rejects with 491).
        if not self._is_buckaroo_giftcard():
            return super()._buckaroo_create_refund(source_tx, refund_tx, client)
        params = self._buckaroo_get_refund_params(source_tx, refund_tx)
        return PaymentService(client).create_payment("giftcards", params).refund()

    def _buckaroo_require_giftcard_brand(self, source_tx):
        return self._buckaroo_require_service_code(
            source_tx,
            _(
                "Giftcard brand unknown on the original transaction. The "
                "original may have been recorded before service-code "
                "tracking was enabled."
            ),
        )

    def _buckaroo_extract_amount_data(self, transaction, payment_data):
        # Partial-pay slices are intentionally < tx.amount.
        if self._is_buckaroo_giftcard():
            return None
        return super()._buckaroo_extract_amount_data(transaction, payment_data)

    def _buckaroo_apply_push_identity(self, transaction, payment_data):
        """First-wins: a remainder push must not overwrite the giftcard tx's
        provider_reference or brand."""
        if not self._is_buckaroo_giftcard():
            return super()._buckaroo_apply_push_identity(transaction, payment_data)
        txn_key = payment_data.transaction_key
        if txn_key and not transaction.provider_reference:
            transaction.provider_reference = txn_key
        service_code = payment_data.service_code
        if service_code and not transaction.buckaroo_official_service_code:
            transaction.buckaroo_official_service_code = service_code

    def _buckaroo_handle_duplicate_push(self, transaction, payment_data):
        """Demote done → cancel when a remainder push (different txn_key)
        cancels or fails. Refund-success pushes fall through to the base,
        which spawns the R- child generically for all buckaroo methods."""
        if not self._is_buckaroo_giftcard():
            return super()._buckaroo_handle_duplicate_push(transaction, payment_data)
        txn_key = payment_data.transaction_key
        same_key = not txn_key or txn_key == transaction.provider_reference
        if same_key:
            _logger.info(
                "Skipping duplicate Buckaroo callback for transaction %s (state=%s)",
                transaction.reference,
                transaction.state,
            )
            return
        if payment_data.is_cancelled() or payment_data.is_failed():
            _logger.info(
                "Giftcard remainder cancelled/failed for %s; demoting tx to cancel",
                transaction.reference,
            )
            transaction._set_canceled(extra_allowed_states=("done",))
            transaction._post_process()
            return
        return super()._buckaroo_handle_duplicate_push(transaction, payment_data)

    def _buckaroo_skip_payment_creation(self, transaction):
        """Whether to veto ``account.payment`` (PBNK) creation for a giftcard tx.

        Inline mode: each leg (giftcard slice + on-site remainder) is an
        independent payment, so the slice KEEPS its PBNK and is refundable from
        Odoo like any other payment.

        Redirect mode: the gateway groups the slice with a GCR remainder child.
        The GCR child (reference ``{ORDER}-GCR-{key}``, created by
        ``_buckaroo_split_remainder_push``) keeps its PBNK; the giftcard slice
        (amount < order total) is refunded via Plaza, so skip its PBNK and avoid
        the post-process race that would otherwise create a stray PBNK with the
        wrong ``amount_available_for_refund``."""
        primary = self._buckaroo_giftcard_primary()
        if not primary:
            return super()._buckaroo_skip_payment_creation(transaction)
        if (primary.buckaroo_official_giftcard_method or "redirect") == "inline":
            return False
        if "-GCR-" in transaction.reference:
            return False  # redirect remainder child, not a partial slice
        order = transaction.sale_order_ids[:1]
        if not order:
            return False
        return transaction.currency_id.compare_amounts(transaction.amount, order.amount_total) < 0

    def _buckaroo_adjust_amount_on_success(self, transaction, payment_data):
        """Shrink the tx to the actually-drawn slice on a partial-pay push."""
        if not self._is_buckaroo_giftcard():
            return super()._buckaroo_adjust_amount_on_success(transaction, payment_data)
        currency = transaction.currency_id
        amount = payment_data.amount
        if not amount:
            return
        if currency.compare_amounts(amount, 0) <= 0:
            return
        if currency.compare_amounts(amount, transaction.amount) >= 0:
            return
        transaction.amount = amount

    def _buckaroo_split_remainder_push(self, transaction, payment_data):
        """Spawn a sibling tx for a hosted-picker remainder push, else return
        an empty recordset. Without the split, the remainder slice never
        materializes and the SO stays draft."""
        if not self._is_buckaroo_giftcard():
            return super()._buckaroo_split_remainder_push(transaction, payment_data)
        if not payment_data.is_success():
            return transaction.browse()
        related = payment_data.get_partial_payment_relation()
        if not related:
            return transaction.browse()
        service_code = payment_data.service_code
        if not service_code:
            return transaction.browse()
        txn_key = payment_data.transaction_key
        if not txn_key:
            return transaction.browse()
        existing = transaction.search(
            [
                ("provider_id", "=", transaction.provider_id.id),
                ("provider_reference", "=", txn_key),
            ],
            limit=1,
        )
        if existing:
            return transaction.browse()
        remainder_method = transaction.provider_id.payment_method_ids.filtered(
            lambda m: m.buckaroo_official_sdk_service_name == service_code
        )[:1]
        if not remainder_method:
            return transaction.browse()
        amount = payment_data.amount or 0.0
        if transaction.currency_id.compare_amounts(amount, 0) <= 0:
            return transaction.browse()
        sibling_ref = f"{transaction.reference}-GCR-{txn_key}"
        sibling_vals = {
            "provider_id": transaction.provider_id.id,
            "payment_method_id": remainder_method.id,
            "reference": sibling_ref,
            "amount": amount,
            "currency_id": transaction.currency_id.id,
            "partner_id": transaction.partner_id.id,
            "operation": "online_redirect",
            "source_transaction_id": transaction.id,
            "sale_order_ids": [Command.set(transaction.sale_order_ids.ids)],
        }
        # Concurrent redelivery may race past the existing-check; rely on the
        # reference unique constraint as the final dedupe.
        try:
            with transaction.env.cr.savepoint():
                return transaction.create(sibling_vals)
        except UniqueViolation:
            return transaction.search(
                [
                    ("provider_id", "=", transaction.provider_id.id),
                    ("reference", "=", sibling_ref),
                ],
                limit=1,
            )

    def _buckaroo_failure_message_hint(self, transaction, response, operation, status_code):
        if (
            operation == "refund"
            and status_code == 690
            and transaction._buckaroo_is_intersolve_refund()
        ):
            return _(
                "Buckaroo Plaza rejected this refund (status 690). Intersolve "
                "giftcard refunds require: (a) Intersolve to enable "
                "partial-refund support on your merchant account (contact "
                "Buckaroo support), and (b) the refund request to include the "
                "cardholder's LastName and Email. Verify both are configured."
            )
        return super()._buckaroo_failure_message_hint(transaction, response, operation, status_code)


class PaymentTransactionGiftcard(models.Model):
    _inherit = "payment.transaction"

    def _buckaroo_giftcard_remainder_group_key(self):
        """Group transaction key when THIS tx is the remainder leg of an inline
        giftcard partial payment — sent as OriginalTransactionKey on a
        PayRemainder so Buckaroo settles it into the giftcard group. Empty for
        the giftcard slice itself and for orders without a giftcard partial."""
        self.ensure_one()
        if self.payment_method_id._is_buckaroo_giftcard():
            return False
        # Field access on an empty recordset returns False.
        return self.sale_order_ids[:1].buckaroo_official_group_transaction_key


class SaleOrderGiftcard(models.Model):
    _inherit = "sale.order"

    buckaroo_official_group_transaction_key = fields.Char(
        string="Buckaroo Group Transaction Key",
        help="Group transaction key returned by Buckaroo on a giftcard partial "
        "payment. Sent as OriginalTransactionKey on the PayRemainder that "
        "settles the open amount, so the gateway groups giftcard + remainder "
        "as one transaction.",
        copy=False,
    )

    def _buckaroo_partial_payment_remainder(self):
        """``amount_total - amount_paid`` when partly paid (>0 paid, < total);
        ``0.0`` otherwise. Single source of truth for the partial-payment
        predicate used in the giftcard controller, sale_order helpers, and QWeb."""
        self.ensure_one()
        currency = self.currency_id
        if currency.compare_amounts(self.amount_paid, 0) <= 0:
            return 0.0
        if currency.compare_amounts(self.amount_paid, self.amount_total) >= 0:
            return 0.0
        return self.amount_total - self.amount_paid

    def _buckaroo_giftcard_done_transactions(self):
        self.ensure_one()
        return self.sudo().transaction_ids.filtered(
            lambda t: (
                t.state == "done"
                and t.provider_id.code == const.PROVIDER_CODE
                and t.payment_method_id._is_buckaroo_giftcard()
            )
        )

    def _buckaroo_has_giftcard_pending_remainder(self):
        self.ensure_one()
        if not self._buckaroo_partial_payment_remainder():
            return False
        return bool(self._buckaroo_giftcard_done_transactions())


class WebsiteGiftcard(models.Model):
    _inherit = "website"

    def _get_and_cache_current_cart(self):
        cached_id = request.session.get(CART_SESSION_CACHE_KEY) if request else None

        cart_sudo = super()._get_and_cache_current_cart()
        if cart_sudo or not cached_id:
            return cart_sudo

        candidate = self.env["sale.order"].sudo().browse(cached_id).exists()
        if not candidate or not candidate._buckaroo_has_giftcard_pending_remainder():
            return cart_sudo

        user = request.env.user
        if user and not user._is_public():
            owner = candidate.partner_id.commercial_partner_id or candidate.partner_id
            shopper = user.partner_id.commercial_partner_id or user.partner_id
            if owner != shopper:
                return cart_sudo

        request.session[CART_SESSION_CACHE_KEY] = candidate.id
        return candidate


class AccountMoveGiftcard(models.Model):
    _inherit = "account.move"

    def _buckaroo_handle_multi_tx_refund(self, transactions):
        """Distribute a credit note amount across giftcard + remainder
        transactions instead of refunding each leg in full. Returns the
        refund action, or ``None`` when no leg is a giftcard tx."""
        self.ensure_one()
        if not any(tx.payment_method_id._is_buckaroo_giftcard() for tx in transactions):
            return None
        currency = self.currency_id
        remaining = abs(self.amount_total)
        available = {
            tx.id: tx.payment_id.amount_available_for_refund if tx.payment_id else 0.0
            for tx in transactions
        }
        sum_available = sum(available.values())
        if currency.compare_amounts(sum_available, remaining) < 0:
            raise UserError(
                _(
                    "Credit note %(cn)s exceeds the total available for refund "
                    "on linked Buckaroo transactions (%(avail)s). Some legs may "
                    "already be refunded or in error. Refund individual payments "
                    "from the payment record instead.",
                    cn=currency.format(remaining),
                    avail=currency.format(sum_available),
                )
            )
        ordered = transactions.sorted(lambda t: available.get(t.id, 0.0), reverse=True)
        refunded_txs_sudo = self.env["payment.transaction"].sudo()
        for tx in ordered:
            if currency.compare_amounts(remaining, 0) <= 0:
                break
            tx_available = available.get(tx.id, 0.0)
            if currency.compare_amounts(tx_available, 0) <= 0:
                continue
            take = min(tx_available, remaining)
            refunded_txs_sudo |= (
                tx.sudo().with_context(payment_backend_action=True)._refund(amount_to_refund=take)
            )
            remaining = currency.round(remaining - take)
        return refunded_txs_sudo._build_action_feedback_notification()
