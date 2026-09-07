# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

from odoo import SUPERUSER_ID, api
from odoo.fields import Command

_logger = logging.getLogger(__name__)

REMOVED_METHOD_CODE = "buckaroo_knaken"
REMOVED_METHOD_XML_ID = "payment_method_knaken"


def migrate(cr, version):
    """Retire the deprecated goSettle payment method.

    The method is archived rather than deleted because
    ``payment_transaction.payment_method_id`` has an ``ON DELETE RESTRICT``
    foreign key: merchants with historical goSettle transactions cannot have
    the record removed.

    Dropping its ``ir.model.data`` row is what keeps the archived record
    alive. Without it, Odoo's end-of-load orphan cleanup
    (``ir.model.data._process_end``) sees an xml id that the data files no
    longer define and unlinks the record with no error handling, which would
    abort the whole upgrade on exactly those databases.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["ir.model.data"].search(
        [
            ("module", "=", "payment_buckaroo_official"),
            ("name", "=", REMOVED_METHOD_XML_ID),
        ]
    ).unlink()

    # ``payment.method.code`` carries no unique constraint, so search for a
    # recordset rather than assuming a single record.
    methods = (
        env["payment.method"]
        .with_context(active_test=False)
        .search([("code", "=", REMOVED_METHOD_CODE)])
    )
    if not methods:
        return

    providers = (
        env["payment.provider"]
        .with_context(active_test=False)
        .search([("payment_method_ids", "in", methods.ids)])
    )
    providers.write(
        {"payment_method_ids": [Command.unlink(method_id) for method_id in methods.ids]}
    )
    methods.write({"active": False})
    _logger.info("Archived deprecated payment method %s.", REMOVED_METHOD_CODE)
