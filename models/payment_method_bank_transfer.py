# Part of Odoo. See LICENSE file for full copyright and licensing details.

from buckaroo.services.payment_service import PaymentService

from odoo import fields, models

from ..helpers.customer import get_customer_data


# Buckaroo service-parameter name → tx field. Looked up via
# ``get_service_parameter`` on both SDK responses and parsed pushes.
_BANK_TRANSFER_PARAM_MAP = {
    "IBAN": "buckaroo_official_bank_iban",
    "BIC": "buckaroo_official_bank_bic",
    "AccountHolderName": "buckaroo_official_bank_account_holder",
    "PaymentReference": "buckaroo_official_bank_payment_reference",
}


class PaymentMethodBankTransfer(models.Model):
    _inherit = "payment.method"

    def _buckaroo_create_payment(self, transaction, client):
        if self.code != "buckaroo_bank_transfer":
            return super()._buckaroo_create_payment(transaction, client)

        params = self._buckaroo_get_payment_params(transaction)
        builder = PaymentService(client).create_payment(
            self.buckaroo_official_sdk_service_name,
            params,
        )

        customer = get_customer_data(transaction.partner_id)
        builder.add_parameter("customeremail", customer["email"])
        builder.add_parameter("customerfirstname", customer["first_name"])
        builder.add_parameter("customerlastname", customer["last_name"])
        if customer["country_code"]:
            builder.add_parameter("customerCountry", customer["country_code"])
        # Have Buckaroo email the bank details to the customer too — the
        # customer may close the browser before reading the on-page details.
        builder.add_parameter("sendmail", "true")

        builder.culture(self._buckaroo_bank_transfer_culture())

        return builder.pay()

    def _buckaroo_bank_transfer_culture(self):
        """The shopper's active-context language as a Buckaroo ``Culture``
        (BCP-47, hyphen form — e.g. ``nl_NL`` → ``nl-NL``). Empty when there's
        no active lang; the SDK then omits the header and the gateway defaults
        to en-US."""
        return (self.env.context.get("lang") or "").replace("_", "-")

    def _buckaroo_handle_no_redirect_response(self, transaction, response):
        """Bank Transfer is async: Buckaroo returns pending (792) without
        an external redirect URL and hands back the bank details inline.
        Persist them, set the tx pending, and route the customer via
        ``/shop/payment/validate`` so they land on ``/shop/confirmation``
        with the order confirmed and the bank details rendered inline."""
        if self.code != "buckaroo_bank_transfer":
            return super()._buckaroo_handle_no_redirect_response(transaction, response)
        if not response.is_pending():
            return None
        details = self._buckaroo_collect_bank_details(response)
        if details:
            transaction.sudo().write(details)
        transaction.provider_reference = response.key
        transaction._set_pending()
        base_url = transaction.provider_id.get_base_url().rstrip("/")
        return {"api_url": f"{base_url}/shop/payment/validate"}

    def _buckaroo_apply_push_metadata(self, transaction, payment_data):
        if self.code != "buckaroo_bank_transfer":
            return super()._buckaroo_apply_push_metadata(transaction, payment_data)
        details = self._buckaroo_collect_bank_details(payment_data)
        if details:
            transaction.write(details)

    @staticmethod
    def _buckaroo_collect_bank_details(source):
        """Read bank-detail fields off any object exposing
        ``get_service_parameter(name)`` (SDK ``PaymentResponse`` or
        ``ParsedPush``). Truthy filter is intentional: status-only refund
        pushes carry no bank details, and an empty string would otherwise
        wipe the previously-stored IBAN/BIC on the tx."""
        out = {}
        for name, field in _BANK_TRANSFER_PARAM_MAP.items():
            value = source.get_service_parameter(name)
            if value:
                out[field] = value
        return out


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    buckaroo_official_bank_iban = fields.Char(
        string="Bank Transfer IBAN",
        readonly=True,
    )
    buckaroo_official_bank_bic = fields.Char(
        string="Bank Transfer BIC",
        readonly=True,
    )
    buckaroo_official_bank_account_holder = fields.Char(
        string="Bank Transfer Account Holder",
        readonly=True,
    )
    buckaroo_official_bank_payment_reference = fields.Char(
        string="Bank Transfer Payment Reference",
        readonly=True,
    )
