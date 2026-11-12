# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Shared sanitizers for Buckaroo wallet (Apple Pay / Google Pay) tokens."""

import re

_RE_CONTROL_CHARS = re.compile(r"[\x00-\x1F\x7F]")


def sanitize_token(value):
    """Trim and length-cap a wallet payment token; ``None`` when empty."""
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    return cleaned[:8192]


def sanitize_customer_name(value):
    """Strip control characters, trim and length-cap; ``None`` when empty."""
    if value is None:
        return None
    cleaned = _RE_CONTROL_CHARS.sub("", str(value)).strip()
    if not cleaned:
        return None
    return cleaned[:128]


def is_amex_network(value):
    """Whether a wallet-reported card network is American Express.

    Apple Pay reports ``AmEx`` (``paymentMethod.network``), Google Pay
    ``AMEX`` (``paymentMethodData.info.cardNetwork``). Buckaroo does not accept
    American Express through either wallet.
    """
    return str(value or "").strip().lower() == "amex"
