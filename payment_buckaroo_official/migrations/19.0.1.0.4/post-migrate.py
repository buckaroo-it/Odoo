# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Allow partial refunds for Riverty.

    ``payment_method_data.xml`` is install-only, so existing databases keep
    ``support_refund = 'full_only'`` and the refund wizard keeps the amount
    read-only until this migration updates it.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    # ``payment.method.code`` carries no unique constraint, so update every match.
    methods = (
        env["payment.method"]
        .with_context(active_test=False)
        .search([("code", "=", "buckaroo_riverty")])
    )
    methods.write({"support_refund": "partial"})
    _logger.info("Enabled partial refunds for %s Riverty payment method(s).", len(methods))
