# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models


class PaymentMethodAlipay(models.Model):
    _inherit = "payment.method"

    def _buckaroo_get_payment_params(self, transaction, description_template=None):
        if self.code != "buckaroo_alipay":
            return super()._buckaroo_get_payment_params(transaction, description_template)

        params = super()._buckaroo_get_payment_params(transaction, description_template)
        params["service_parameters"] = {"UseMobileView": False}
        return params
