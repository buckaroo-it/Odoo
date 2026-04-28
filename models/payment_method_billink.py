# Part of Odoo. See LICENSE file for full copyright and licensing details.

from datetime import datetime

from buckaroo.services.payment_service import PaymentService

from odoo import fields, models

from ..helpers.articles import get_order_articles
from ..helpers.customer import (
    get_billing_partner,
    get_customer_data,
    get_shipping_partner,
)


class PaymentMethodBillink(models.Model):
    _inherit = 'payment.method'

    @staticmethod
    def _format_billink_articles(articles):
        """Map generic article dicts to Billink's API article format.

        Billink expects PascalCase keys and quantity as a string.
        """
        return [
            {
                'Identifier': a['identifier'],
                'Description': a['description'],
                'Quantity': str(int(a['quantity'])),
                'GrossUnitPriceIncl': a['unit_price_incl'],
                'GrossUnitPriceExcl': a['unit_price_excl'],
                'VatPercentage': a['vat_percentage'],
            }
            for a in articles
        ]

    @staticmethod
    def _format_billink_customer(data):
        """Map generic customer data to Billink's API customer format."""
        category = 'B2B' if data['is_b2b'] else 'B2C'

        customer = {
            'Category': category,
            'FirstName': data['first_name'],
            'LastName': data['last_name'],
            'Initials': data['initials'],
            'Street': data['street_name'],
            'StreetNumber': data['house_number'],
            'PostalCode': data['postal_code'],
            'City': data['city'],
            'Country': data['country_code'],
            'Email': data['email'],
            'MobilePhone': data['phone'],
            'Salutation': 'Unknown',
        }

        if data['is_b2b']:
            customer['CareOf'] = data['company_name']
            customer['ChamberOfCommerce'] = data['chamber_of_commerce']
        else:
            customer['CareOf'] = ''

        return customer

    @staticmethod
    def _get_birthdate_from_session():
        """Read and consume the Billink birthdate from the HTTP session.

        Returns the date in DD-MM-YYYY format (Billink API), or ``''``.
        """
        from odoo.http import request as http_request  # noqa: PLC0415
        if not http_request:
            return ''
        try:
            birthdate = http_request.session.pop('buckaroo_billink_birthdate', None)
        except RuntimeError:
            return ''
        if not birthdate:
            return ''
        try:
            return datetime.strptime(birthdate, '%Y-%m-%d').strftime('%d-%m-%Y')
        except ValueError:
            return ''

    def _buckaroo_create_payment(self, transaction, client):
        """Billink flow: add article and customer data, then .pay()."""
        if self.code != 'billink':
            return super()._buckaroo_create_payment(transaction, client)

        params = self._buckaroo_get_payment_params(transaction)
        builder = PaymentService(client).create_payment(
            self._buckaroo_get_sdk_service_name(), params,
        )

        # Articles
        articles = get_order_articles(transaction)
        if articles:
            builder.add_parameter('article', self._format_billink_articles(articles))

        # Billing & shipping customer
        billing_partner = get_billing_partner(transaction)
        shipping_partner = get_shipping_partner(transaction)

        billing_data = get_customer_data(billing_partner)
        billing_customer = self._format_billink_customer(billing_data)
        billing_customer['BirthDate'] = self._get_birthdate_from_session()
        builder.add_parameter('billingCustomer', [billing_customer])

        if shipping_partner == billing_partner:
            shipping_data = billing_data
        else:
            shipping_data = get_customer_data(shipping_partner)
        shipping_customer = self._format_billink_customer(shipping_data)
        builder.add_parameter('shippingCustomer', [shipping_customer])

        return builder.pay()


class ResPartner(models.Model):
    _inherit = 'res.partner'

    buckaroo_billink_birthdate = fields.Date(
        string="Billink Date of Birth",
        help="Saved from the last Billink checkout to prefill on next order.",
    )
