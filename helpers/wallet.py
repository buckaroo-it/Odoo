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
