# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Shared BNPL customer helpers."""

import re


_RE_NUMBER_END = re.compile(r"^(.*?)\s+(\d+.*)$")
_RE_NUMBER_START = re.compile(r"^(\d+\S*)\s+(.+)$")
_RE_HOUSE_NUMBER = re.compile(r"^\s*(\d+)\s*([A-Za-z][\w\-/]*)?\s*$")


def sanitize_phone(value):
    """Strip non-digit characters; BNPL APIs reject ``+`` and whitespace."""
    if not value:
        return ""
    return re.sub(r"\D", "", str(value))


def pop_session_value(key):
    """Read and consume a session value, ``''`` when no request context."""
    from odoo.http import request as http_request  # noqa: PLC0415

    if not http_request:
        return ""
    try:
        value = http_request.session.pop(key, None)
    except RuntimeError:
        return ""
    return value or ""


def is_at_least_age(dob, min_age, today=None):
    """``True`` when *dob* is at least *min_age* years before *today*.

    ``(today - dob).days // 365`` rounds up across leap-day boundaries
    and lets 17y 364d past as 18 — use year-difference with a (month,
    day) tiebreak instead.
    """
    from datetime import date as _date  # noqa: PLC0415

    today = today or _date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return age >= min_age


def resolve_birthdate(transaction, session_key, partner_field, *, missing_error=None):
    """Resolve a DD-MM-YYYY birthdate from session → partner.

    *session_key* is consumed via :func:`pop_session_value`; the partner
    fallback reads ``transaction.partner_id.<partner_field>``. Returns
    ``''`` when neither is present unless *missing_error* is supplied,
    in which case :class:`ValidationError` is raised with that message.
    """
    from datetime import datetime as _datetime  # noqa: PLC0415

    raw = pop_session_value(session_key)
    if raw:
        try:
            return _datetime.strptime(raw, "%Y-%m-%d").strftime("%d-%m-%Y")
        except ValueError:
            pass
    partner_dob = getattr(transaction.partner_id, partner_field, None)
    if partner_dob:
        return partner_dob.strftime("%d-%m-%Y")
    if missing_error is not None:
        from odoo.exceptions import ValidationError  # noqa: PLC0415

        raise ValidationError(missing_error)
    return ""


def split_house_number(raw):
    """``"1A"`` → ``("1", "A")``; ``"42"`` → ``("42", "")``.

    Falls back to ``(raw, "")`` on no match — callers downstream
    (Riverty's ``StreetNumber``) require a non-empty first slot.
    """
    if not raw:
        return ("", "")
    match = _RE_HOUSE_NUMBER.match(str(raw))
    if not match:
        return (str(raw).strip(), "")
    return (match.group(1), match.group(2) or "")


def parse_street(street):
    """Split a street string into ``(street_name, house_number)``.

    Handles number-at-end ("Keizersgracht 424") and number-at-start
    ("10B Downing Street").
    """
    if not street:
        return ("", "")
    s = street.strip()
    match = _RE_NUMBER_END.search(s)
    if match:
        return (match.group(1).strip(), match.group(2).strip())
    match = _RE_NUMBER_START.search(s)
    if match:
        return (match.group(2).strip(), match.group(1).strip())
    return (s, "")


def is_b2b(partner):
    return bool(partner.is_company or getattr(partner, "commercial_company_name", None))


def get_customer_data(partner):
    """Snake_case dict of fields shared across BNPL methods."""
    street_name, house_number = parse_street(partner.street or "")
    if not house_number and getattr(partner, "street2", None):
        house_number = partner.street2.strip()

    full_name = (partner.name or "").strip()
    name_parts = full_name.split(" ", 1)
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""
    initials = "".join(p[0].upper() + "." for p in full_name.split() if p)

    b2b = is_b2b(partner)

    # Riverty NL/BE prefers ``MobilePhone``; fall back to landline.
    phone = getattr(partner, "mobile", None) or partner.phone or ""
    data = {
        "first_name": first_name,
        "last_name": last_name,
        "initials": initials,
        "street_name": street_name,
        "house_number": house_number,
        "postal_code": partner.zip or "",
        "city": partner.city or "",
        "country_code": partner.country_id.code if partner.country_id else "",
        "email": partner.email or "",
        "phone": phone,
        "is_b2b": b2b,
        "company_name": "",
        "chamber_of_commerce": "",
        "vat_number": "",
    }

    if b2b:
        data["company_name"] = (
            getattr(partner, "commercial_company_name", None)
            or getattr(partner, "company_name", None)
            or ""
        )
        data["chamber_of_commerce"] = getattr(partner, "company_registry", None) or ""
        data["vat_number"] = getattr(partner, "vat", None) or ""

    return data


def validate_bnpl_birthdate(
    kwargs, *, birthdate_kwarg, session_key, partner_field, missing_msg, invalid_msg, underage_msg
):
    """Pop, parse and validate a BNPL birthdate from controller *kwargs*.

    Raises :class:`ValidationError` with the supplied messages on
    missing / unparseable / underage input. On success persists the
    raw string to ``request.session[session_key]`` and the parsed
    date to ``request.env.user.partner_id.<partner_field>`` for the
    next checkout to prefill.
    """
    from datetime import datetime as _datetime  # noqa: PLC0415
    from odoo.exceptions import ValidationError  # noqa: PLC0415
    from odoo.http import request as http_request  # noqa: PLC0415

    raw = kwargs.pop(birthdate_kwarg, None)
    if not raw:
        raise ValidationError(missing_msg)
    try:
        dob = _datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError(invalid_msg)
    if not is_at_least_age(dob, 18):
        raise ValidationError(underage_msg)

    http_request.session[session_key] = raw
    user = http_request.env.user
    if not user._is_public():
        setattr(user.partner_id.sudo(), partner_field, dob)
    return dob


def get_billing_partner(transaction):
    return transaction.partner_id


def get_shipping_partner(transaction):
    """Falls back to billing when shipping is unset or equal."""
    partner = transaction.partner_id
    for order in transaction.sale_order_ids:
        if order.partner_shipping_id and order.partner_shipping_id != partner:
            return order.partner_shipping_id
    return partner


def resolve_bnpl_customer_data(transaction):
    """Return ``(billing_data, shipping_data, same_address)`` for *transaction*.

    When the shipping partner equals the billing partner, *shipping_data*
    is the same dict as *billing_data* (identity, not a copy) so callers
    may key-mutate one and have both reflect it.
    """
    billing_partner = get_billing_partner(transaction)
    shipping_partner = get_shipping_partner(transaction)
    billing_data = get_customer_data(billing_partner)
    same_address = shipping_partner == billing_partner
    shipping_data = billing_data if same_address else get_customer_data(shipping_partner)
    return billing_data, shipping_data, same_address
