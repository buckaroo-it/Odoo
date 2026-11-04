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

    def _check_shop_address_completion(self, order_sudo, **kwargs):
        if self.env.context.get("buckaroo_paypal_express"):
            # PayPal gives the full address only after capture, when
            # `_buckaroo_paypal_write_address` replaces the express stub.
            order_sudo = order_sudo.with_context(buckaroo_skip_address_completeness=True)
        return super()._check_shop_address_completion(order_sudo, **kwargs)
