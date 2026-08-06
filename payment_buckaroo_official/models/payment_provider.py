# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

import requests as req_lib
from buckaroo.http.client import BuckarooApiError

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..utils import const

_logger = logging.getLogger(__name__)


class PaymentProvider(models.Model):
    _inherit = "payment.provider"

    code = fields.Selection(
        selection_add=[(const.PROVIDER_CODE, "Buckaroo")],
        ondelete={const.PROVIDER_CODE: "set default"},
    )
    buckaroo_official_website_key = fields.Char(
        string="Store Key",
        help="The key used to identify the store with Buckaroo Official.",
        required_if_provider=const.PROVIDER_CODE,
        copy=False,
    )
    buckaroo_official_secret_key = fields.Char(
        string="Secret Key",
        required_if_provider=const.PROVIDER_CODE,
        copy=False,
        groups="base.group_system",
    )
    buckaroo_official_transaction_description = fields.Char(
        string="Transaction Description",
        help="Description sent with payment transactions. Supports placeholders: "
        "{order_number}, {shop_name}. "
        "Leave empty to use the transaction reference.",
    )
    buckaroo_official_refund_description = fields.Char(
        string="Refund Description",
        help="Description sent with refund requests. Supports placeholders: "
        "{order_number}, {shop_name}. "
        "Leave empty to use the transaction description above.",
    )

    def _compute_feature_support_fields(self):
        super()._compute_feature_support_fields()
        self.filtered(lambda p: p.code == const.PROVIDER_CODE).update(
            {
                "support_refund": "partial",
                "support_manual_capture": "full_only",
                "support_express_checkout": True,
            }
        )

    def _get_default_payment_method_codes(self):
        self.ensure_one()
        if self.code != const.PROVIDER_CODE:
            return super()._get_default_payment_method_codes()
        return const.DEFAULT_PAYMENT_METHOD_CODES

    def _buckaroo_official_get_client(self):
        """Instantiate and return a ``BuckarooClient`` for this provider."""
        self.ensure_one()
        from buckaroo._buckaroo_client import BuckarooClient

        if self.state == "test":
            mode = "test"
        elif self.state == "enabled":
            mode = "live"
        else:
            raise UserError(_("Cannot process payment: the Buckaroo provider is disabled."))
        return BuckarooClient(
            self.buckaroo_official_website_key,
            self.buckaroo_official_secret_key,
            mode=mode,
        )

    def action_buckaroo_official_test_connection(self):
        """Test the connection to the Buckaroo API using the configured credentials."""
        self.ensure_one()
        try:
            client = self._buckaroo_official_get_client()
            is_valid = client.confirm_credential()
        except (BuckarooApiError, req_lib.RequestException) as e:
            raise UserError(_("Connection failed: %s", e))

        if not is_valid:
            raise UserError(_("Connection failed: the Store Key or Secret Key is incorrect."))

        mode = self.state
        _logger.info("Buckaroo test connection successful (mode=%s).", mode)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Connection successful!"),
                "message": _("Your Buckaroo %s credentials are valid.", mode),
                "type": "success",
                "sticky": False,
            },
        }
