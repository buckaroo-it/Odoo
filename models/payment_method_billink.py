# Part of Odoo. See LICENSE file for full copyright and licensing details.

from buckaroo.services.payment_service import PaymentService

from odoo import fields, models

from ..helpers.articles import get_order_articles
from ..helpers.customer import (
    resolve_birthdate,
    resolve_bnpl_customer_data,
    sanitize_phone,
)


class PaymentMethodBillink(models.Model):
    _inherit = "payment.method"

    @staticmethod
    def _format_billink_articles(articles):
        return [
            {
                "Identifier": a["identifier"],
                "Description": a["description"],
                "Quantity": str(int(a["quantity"])),
                "GrossUnitPriceIncl": a["unit_price_incl"],
                "GrossUnitPriceExcl": a["unit_price_excl"],
                "VatPercentage": a["vat_percentage"],
            }
            for a in articles
        ]

    @staticmethod
    def _format_billink_customer(data):
        category = "B2B" if data["is_b2b"] else "B2C"

        customer = {
            "Category": category,
            "FirstName": data["first_name"],
            "LastName": data["last_name"],
            "Initials": data["initials"],
            "Street": data["street_name"],
            "StreetNumber": data["house_number"],
            "PostalCode": data["postal_code"],
            "City": data["city"],
            "Country": data["country_code"],
            "Email": data["email"],
            "MobilePhone": sanitize_phone(data["phone"]),
            "Salutation": "Unknown",
        }

        if data["is_b2b"]:
            customer["CareOf"] = data["company_name"]
            customer["ChamberOfCommerce"] = data["chamber_of_commerce"]
        else:
            customer["CareOf"] = ""

        return customer

    def _buckaroo_create_payment(self, transaction, client):
        if self.code != "buckaroo_billink":
            return super()._buckaroo_create_payment(transaction, client)

        params = self._buckaroo_get_payment_params(transaction)
        builder = PaymentService(client).create_payment(
            self.buckaroo_official_sdk_service_name,
            params,
        )

        articles = get_order_articles(transaction)
        if articles:
            builder.add_parameter("article", self._format_billink_articles(articles))

        billing_data, shipping_data, _same = resolve_bnpl_customer_data(transaction)
        billing_customer = self._format_billink_customer(billing_data)
        billing_customer["BirthDate"] = resolve_birthdate(
            transaction,
            "buckaroo_billink_birthdate",
            "buckaroo_billink_birthdate",
        )
        builder.add_parameter("billingCustomer", [billing_customer])

        shipping_customer = self._format_billink_customer(shipping_data)
        builder.add_parameter("shippingCustomer", [shipping_customer])

        return builder.pay()


class ResPartner(models.Model):
    _inherit = "res.partner"

    buckaroo_billink_birthdate = fields.Date(
        string="Billink Date of Birth",
        help="Saved from the last Billink checkout to prefill on next order.",
    )
