# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Shared BNPL customer helpers.

Module-level pure functions used across BNPL methods (Billink today;
Klarna/Riverty/In3 later). They pull generic customer data out of an
Odoo ``res.partner`` record in a snake_case dict; each method maps this
dict to its own API shape (PascalCase + naming quirks) in its own file.
"""

import re


_RE_NUMBER_END = re.compile(r'^(.*?)\s+(\d+.*)$')
_RE_NUMBER_START = re.compile(r'^(\d+\S*)\s+(.+)$')


def parse_street(street):
    """Split a street string into ``(street_name, house_number)``.

    Handles number-at-end ("Keizersgracht 424") and number-at-start
    ("10B Downing Street"). Returns ``(street, '')`` when no number.
    """
    if not street:
        return ('', '')
    s = street.strip()
    match = _RE_NUMBER_END.search(s)
    if match:
        return (match.group(1).strip(), match.group(2).strip())
    match = _RE_NUMBER_START.search(s)
    if match:
        return (match.group(2).strip(), match.group(1).strip())
    return (s, '')


def is_b2b(partner):
    """Return ``True`` when *partner* represents a business (B2B) customer."""
    return bool(
        partner.is_company
        or getattr(partner, 'commercial_company_name', None)
    )


def get_customer_data(partner):
    """Extract generic customer data from an Odoo ``res.partner`` record.

    Returns a snake_case dict with fields shared across BNPL methods
    (name, address, contact, B2B flag + company fields when applicable).
    """
    street_name, house_number = parse_street(partner.street or '')
    # Odoo often stores the house number in street2
    if not house_number and getattr(partner, 'street2', None):
        house_number = partner.street2.strip()

    full_name = (partner.name or '').strip()
    name_parts = full_name.split(' ', 1)
    first_name = name_parts[0] if name_parts else ''
    last_name = name_parts[1] if len(name_parts) > 1 else ''
    initials = ''.join(p[0].upper() + '.' for p in full_name.split() if p)

    b2b = is_b2b(partner)

    data = {
        'first_name': first_name,
        'last_name': last_name,
        'initials': initials,
        'street_name': street_name,
        'house_number': house_number,
        'postal_code': partner.zip or '',
        'city': partner.city or '',
        'country_code': partner.country_id.code if partner.country_id else '',
        'email': partner.email or '',
        'phone': partner.phone or '',
        'is_b2b': b2b,
        'company_name': '',
        'chamber_of_commerce': '',
        'vat_number': '',
    }

    if b2b:
        data['company_name'] = (
            getattr(partner, 'commercial_company_name', None)
            or getattr(partner, 'company_name', None)
            or ''
        )
        data['chamber_of_commerce'] = getattr(partner, 'company_registry', None) or ''
        data['vat_number'] = getattr(partner, 'vat', None) or ''

    return data


def get_billing_partner(transaction):
    """Return the billing partner for *transaction*."""
    return transaction.partner_id


def get_shipping_partner(transaction):
    """Return the shipping partner for *transaction*.

    Falls back to the billing partner when no separate shipping address
    is set or when the shipping address equals the billing address.
    """
    partner = transaction.partner_id
    for order in transaction.sale_order_ids:
        if order.partner_shipping_id and order.partner_shipping_id != partner:
            return order.partner_shipping_id
    return partner
