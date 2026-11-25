# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _get_translation_frontend_modules_name(cls):
        # Shop pages load JS translations only for the modules listed here;
        # without it our wallet and payment-form `_t` strings stay English.
        mods = super()._get_translation_frontend_modules_name()
        return mods + ["payment_buckaroo_official"]
