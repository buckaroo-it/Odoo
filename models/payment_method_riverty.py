# Part of Odoo. See LICENSE file for full copyright and licensing details.

from buckaroo.services.payment_service import PaymentService

from odoo import _, fields, models
from odoo.exceptions import ValidationError

from ..helpers.articles import get_order_articles
from ..helpers.customer import (
    pop_session_value,
    resolve_birthdate,
    resolve_bnpl_customer_data,
    sanitize_phone,
    split_house_number,
)


class PaymentMethodRiverty(models.Model):
    _inherit = "payment.method"

    buckaroo_official_riverty_authorize = fields.Selection(
        string="Riverty Payment Action",
        selection=[("pay", "Pay"), ("authorize", "Authorize")],
        default="pay",
        help="Choose whether to capture the payment immediately ('Pay') or "
        "only authorize it for later manual capture ('Authorize').",
    )

    @staticmethod
    def _format_riverty_articles(articles, base_url=""):
        """Map generic article dicts to Riverty's API article format.

        ``Quantity`` is wire-typed as integer; clamping rounded values to
        1 prevents fractional-kg lines from silently freeing the article.
        """
        formatted = []
        for article in articles:
            quantity = max(1, int(round(article["quantity"])))
            entry = {
                "Identifier": article["identifier"],
                "Description": article["description"],
                "Quantity": str(quantity),
                "GrossUnitPrice": article["unit_price_incl"],
                "VatPercentage": article["vat_percentage"],
                "Type": {
                    "product": "PhysicalArticle",
                    "shipping": "ShippingFee",
                    "rounding": "Surcharge",
                }.get(article.get("type"), "PhysicalArticle"),
            }
            product = article.get("product")
            # Skip the ``image_1024`` truthiness check — it would load
            # the binary blob for every product line. Odoo's /web/image
            # route serves a placeholder for products without an image.
            # ``.png`` slug satisfies Riverty's URL-extension regex.
            if base_url and article.get("type") == "product" and product:
                entry["ImageUrl"] = "%s/web/image/product.product/%s/image_1024/product.png" % (
                    base_url,
                    product.id,
                )
            formatted.append(entry)
        return formatted

    @staticmethod
    def _format_riverty_customer(data):
        """Map generic customer data to Riverty's API customer format.

        Caller adds ``BirthDate`` / ``Salutation`` after; both are
        derived from session-or-partner state shared between billing and
        shipping groups.
        """
        category = "Company" if data["is_b2b"] else "Person"
        number, suffix = split_house_number(data["house_number"])
        phone = sanitize_phone(data["phone"])

        customer = {
            "Category": category,
            "FirstName": data["first_name"],
            "LastName": data["last_name"],
            "Street": data["street_name"],
            "StreetNumber": number,
            "StreetNumberAdditional": suffix,
            "PostalCode": data["postal_code"],
            "City": data["city"],
            "Country": data["country_code"],
            "Email": data["email"],
            # Same number on both slots — for NL/BE Riverty needs at
            # least one of MobilePhone / Phone, regardless of which
            # field the customer's record populated.
            "MobilePhone": phone,
            "Phone": phone,
        }

        if data["is_b2b"]:
            customer["CompanyName"] = data["company_name"]
            customer["IdentificationNumber"] = data["chamber_of_commerce"]

        return customer

    def _buckaroo_get_payment_action(self):
        self.ensure_one()
        if self.code == "buckaroo_riverty" and self.buckaroo_official_riverty_authorize == "authorize":
            return "authorize"
        return super()._buckaroo_get_payment_action()

    def _buckaroo_create_payment(self, transaction, client):
        if self.code != "buckaroo_riverty":
            return super()._buckaroo_create_payment(transaction, client)

        articles = get_order_articles(transaction)
        if not articles:
            raise ValidationError(
                _(
                    "Riverty requires order articles. Create the transaction "
                    "from a sale order with at least one line."
                )
            )

        birthdate = resolve_birthdate(
            transaction,
            "buckaroo_riverty_birthdate",
            "buckaroo_riverty_birthdate",
            missing_error=_("Please provide a date of birth to proceed with Riverty."),
        )
        salutation = (
            pop_session_value("buckaroo_riverty_salutation")
            or transaction.partner_id.buckaroo_riverty_salutation
            or ""
        )

        billing_data, shipping_data, _same = resolve_bnpl_customer_data(transaction)

        if billing_data["country_code"] in ("NL", "BE") and not salutation:
            raise ValidationError(_("Please select a salutation to proceed with Riverty."))

        params = self._buckaroo_get_payment_params(transaction)
        builder = PaymentService(client).create_payment(
            self.buckaroo_official_sdk_service_name,
            params,
        )

        base_url = transaction.provider_id.get_base_url().rstrip("/")
        builder.add_parameter(
            "article",
            self._format_riverty_articles(articles, base_url),
        )

        # Same birthdate/salutation flow on both groups — Riverty
        # rejects mixed-country billing/shipping anyway.
        for slot, data in (("billingCustomer", billing_data), ("shippingCustomer", shipping_data)):
            customer = self._format_riverty_customer(data)
            customer["BirthDate"] = birthdate
            if salutation:
                customer["Salutation"] = salutation
            builder.add_parameter(slot, [customer])

        if self.buckaroo_official_riverty_authorize == "authorize":
            return builder.authorize()
        return builder.pay()

    def _buckaroo_create_refund(self, source_tx, refund_tx, client):
        """Riverty refund: full only.

        Partial refunds need an article-level breakdown that Odoo's
        amount-based refund flow can't supply; raising early avoids a
        491 mid-checkout.
        """
        if self.code != "buckaroo_riverty":
            return super()._buckaroo_create_refund(source_tx, refund_tx, client)
        if round(abs(refund_tx.amount), 2) != round(source_tx.amount, 2):
            raise ValidationError(
                _(
                    "Riverty partial refunds are not supported via Odoo. "
                    "For partial refunds, use Buckaroo Plaza."
                )
            )
        return super()._buckaroo_create_refund(source_tx, refund_tx, client)


class ResPartner(models.Model):
    _inherit = "res.partner"

    buckaroo_riverty_birthdate = fields.Date(
        string="Riverty Date of Birth",
        help="Saved from the last Riverty checkout to prefill on next order.",
    )
    buckaroo_riverty_salutation = fields.Selection(
        selection=[("Mr", "Mr"), ("Mrs", "Mrs"), ("Miss", "Miss")],
        string="Riverty Salutation",
        help="Saved from the last Riverty checkout to prefill on next order.",
    )
