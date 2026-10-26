# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models


class WebsiteCheckoutStep(models.Model):
    _inherit = "website.checkout.step"

    def validate_checkout_progress(self, step_href, order_sudo, **kwargs):
        # Runs under the order's FOR NO KEY UPDATE NOWAIT lock taken by
        # /shop/payment/transaction, so concurrent picks serialise. The surcharge
        # is synced after the price check so the amount charged includes it.
        redirect = super().validate_checkout_progress(step_href, order_sudo, **kwargs)
        if not redirect and step_href == "/shop/payment/transaction":
            order_sudo._buckaroo_sync_surcharge_from_context()
        return redirect
