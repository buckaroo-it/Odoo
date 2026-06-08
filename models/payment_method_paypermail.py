# Part of Odoo. See LICENSE file for full copyright and licensing details.

from buckaroo.services.payment_service import PaymentService

from odoo import _, fields, models

from ..helpers.customer import get_customer_data, pop_session_value


class PaymentMethodPayPerEmail(models.Model):
    _inherit = "payment.method"

    def _buckaroo_create_payment(self, transaction, client):
        """Pay Per Email sends a PaymentInvitation: Buckaroo emails the shopper
        a payment link instead of returning an inline redirect. Customer email
        and name come from the order partner."""
        if self.code != "buckaroo_paypermail":
            return super()._buckaroo_create_payment(transaction, client)

        params = self._buckaroo_get_payment_params(transaction)
        builder = PaymentService(client).create_payment(
            self.buckaroo_official_sdk_service_name,
            params,
        )

        # Buckaroo requires both name parts; a single-word partner name leaves
        # last_name empty, so fall back to the first name.
        customer = get_customer_data(transaction.partner_id)
        builder.add_parameter("CustomerEmail", customer["email"])
        builder.add_parameter("CustomerFirstName", customer["first_name"])
        builder.add_parameter("CustomerLastName", customer["last_name"] or customer["first_name"])

        # Buckaroo requires CustomerGender. The shopper's pick (session) or the
        # partner's saved value wins; default to 0 (Unknown) when neither is set
        # so the invitation never fails on a missing field.
        gender = (
            pop_session_value("buckaroo_paypermail_gender")
            or transaction.partner_id.buckaroo_paypermail_gender
        )
        builder.add_parameter("CustomerGender", int(gender) if gender else 0)

        # Scope the hosted paylink to the methods enabled on the Odoo store
        # (in display order) rather than every method the merchant is
        # subscribed to; Pay Per Email itself can't pay its own invitation.
        enabled = transaction.provider_id.payment_method_ids.filtered(
            lambda m: m.active
            and m.is_primary
            and m.code != "buckaroo_paypermail"
            and m.buckaroo_official_sdk_service_name
        ).sorted("sequence")
        allowed = ",".join(enabled.mapped("buckaroo_official_sdk_service_name"))
        if allowed:
            builder.add_parameter("PaymentMethodsAllowed", allowed)

        return builder.execute_action("PaymentInvitation")

    def _buckaroo_handle_no_redirect_response(self, transaction, response):
        """PaymentInvitation returns pending (the shopper pays later via the
        emailed link) with no external redirect. Set the tx pending and route
        the customer to ``/shop/payment/validate`` so the order is confirmed."""
        if self.code != "buckaroo_paypermail":
            return super()._buckaroo_handle_no_redirect_response(transaction, response)
        if not response.is_pending():
            return None
        transaction.provider_reference = response.key
        transaction._set_pending()
        base_url = transaction.provider_id.get_base_url().rstrip("/")
        return {"api_url": f"{base_url}/shop/payment/validate"}

    def _buckaroo_apply_push_identity(self, transaction, payment_data):
        """The invitation is paid later via a real method (iDEAL, card, ...).
        The success push names that method in ``service_code`` and carries every
        involved transaction key (comma-joined); the last is the actual payment.
        Record the real method and repoint the reference at it so the tx,
        refunds and order reflect what was used rather than 'payperemail'.
        Non-success pushes leave the pending invitation reference untouched."""
        if self.code != "buckaroo_paypermail":
            return super()._buckaroo_apply_push_identity(transaction, payment_data)
        if not payment_data.is_success():
            return
        txn_key = payment_data.transaction_key
        if txn_key:
            transaction.provider_reference = txn_key.split(",")[-1].strip()
        service_code = payment_data.service_code
        if service_code and service_code.lower() != "payperemail":
            transaction.buckaroo_official_service_code = service_code
            transaction._log_message_on_linked_documents(
                _("Pay Per Email paid via %s.", service_code)
            )

    def _buckaroo_create_refund(self, source_tx, refund_tx, client):
        """Refund through the method the shopper actually paid with: the PPE
        ``payperemail`` service only supports PaymentInvitation, so the refund
        must be keyed to the recorded underlying method (iDEAL, card, ...)."""
        if self.code != "buckaroo_paypermail":
            return super()._buckaroo_create_refund(source_tx, refund_tx, client)
        service_name = self._buckaroo_require_service_code(
            source_tx,
            _(
                "Pay Per Email cannot be refunded until the paid method is "
                "known. The push that records the actual payment method has "
                "not arrived yet."
            ),
        )
        params = self._buckaroo_get_refund_params(source_tx, refund_tx)
        return (
            PaymentService(client)
            .create_payment(service_name, params)
            .refund()
        )


class ResPartner(models.Model):
    _inherit = "res.partner"

    buckaroo_paypermail_gender = fields.Selection(
        selection=[("1", "Male"), ("2", "Female")],
        string="Pay Per Email Gender",
        help="Saved from the last Pay Per Email checkout to prefill on next order.",
    )
