# Part of Odoo. See LICENSE file for full copyright and licensing details.

from buckaroo.services.payment_service import PaymentService

from odoo import _, fields, models
from odoo.exceptions import ValidationError

from ..helpers.articles import get_order_articles
from ..helpers.customer import (
    resolve_birthdate,
    resolve_bnpl_customer_data,
    sanitize_phone,
    split_house_number,
)


class PaymentMethodIn3(models.Model):
    _inherit = "payment.method"

    @staticmethod
    def _format_in3_articles(articles):
        return [
            {
                "Identifier": a["identifier"],
                "Description": a["description"],
                "Quantity": str(int(a["quantity"])),
                "GrossUnitPrice": a["unit_price_incl"],
                "VatPercentage": a["vat_percentage"],
            }
            for a in articles
        ]

    @staticmethod
    def _format_in3_customer(data):
        """Map generic customer data to In3's API customer format.

        Caller adds slot-specific fields (``CustomerNumber``, ``Email``,
        ``Phone``, ``BirthDate`` for billing; ``CareOf`` for shipping).
        """
        category = "B2B" if data["is_b2b"] else "B2C"
        number, suffix = split_house_number(data["house_number"])
        customer = {
            "Category": category,
            "FirstName": data["first_name"],
            "LastName": data["last_name"],
            "Initials": data["initials"],
            "Street": data["street_name"],
            "StreetNumber": number,
            "PostalCode": data["postal_code"],
            "City": data["city"],
            "CountryCode": data["country_code"],
        }
        if suffix:
            customer["StreetNumberSuffix"] = suffix
        if data["is_b2b"]:
            customer["CompanyName"] = data["company_name"]
            customer["CocNumber"] = data["chamber_of_commerce"]
        return customer

    def _buckaroo_create_payment(self, transaction, client):
        if self.code != "in3":
            return super()._buckaroo_create_payment(transaction, client)

        articles = get_order_articles(transaction)
        if not articles:
            raise ValidationError(
                _(
                    "In3 requires order articles. Create the transaction "
                    "from a sale order with at least one line."
                )
            )

        birthdate = resolve_birthdate(
            transaction,
            "buckaroo_in3_birthdate",
            "buckaroo_in3_birthdate",
            out_format="%Y-%m-%d",
            missing_error=_("Please provide a date of birth to proceed with In3."),
        )

        billing_data, shipping_data, _same = resolve_bnpl_customer_data(transaction)

        params = self._buckaroo_get_payment_params(transaction)
        builder = PaymentService(client).create_payment(
            self._buckaroo_get_sdk_service_name(),
            params,
        )
        builder.add_parameter("article", self._format_in3_articles(articles))

        billing_partner = transaction.partner_id
        billing_customer = self._format_in3_customer(billing_data)
        billing_customer["CustomerNumber"] = str(
            billing_partner.commercial_partner_id.id or billing_partner.id or ""
        )
        billing_customer["Email"] = billing_data["email"]
        billing_customer["Phone"] = sanitize_phone(billing_data["phone"])
        billing_customer["BirthDate"] = birthdate
        builder.add_parameter("billingCustomer", [billing_customer])

        shipping_customer = self._format_in3_customer(shipping_data)
        shipping_customer["CareOf"] = (billing_partner.name or "").strip()
        builder.add_parameter("shippingCustomer", [shipping_customer])

        return builder.pay()


class ResPartner(models.Model):
    _inherit = "res.partner"

    buckaroo_in3_birthdate = fields.Date(
        string="In3 Date of Birth",
        help="Saved from the last In3 checkout to prefill on next order.",
    )
