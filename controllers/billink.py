# Part of Odoo. See LICENSE file for full copyright and licensing details.

from datetime import date, datetime

from odoo import _
from odoo.exceptions import ValidationError
from odoo.http import request

from odoo.addons.website_sale.controllers.payment import PaymentPortal


class BillinkPaymentPortal(PaymentPortal):

    def shop_payment_transaction(self, order_id, access_token, **kwargs):
        payment_method_id = kwargs.get('payment_method_id')
        if payment_method_id:
            pm = request.env['payment.method'].sudo().browse(int(payment_method_id))
            if pm.code == 'billink':
                if not kwargs.pop('billink_tc_accepted', False):
                    raise ValidationError(
                        _("Please accept the Billink Terms and Conditions to proceed.")
                    )
                birthdate = kwargs.pop('billink_birthdate', None)
                if not birthdate:
                    raise ValidationError(
                        _("Please enter your date of birth to proceed with Billink.")
                    )
                try:
                    dob = datetime.strptime(birthdate, '%Y-%m-%d').date()
                except ValueError:
                    raise ValidationError(_("Invalid date of birth."))
                age = (date.today() - dob).days // 365
                if age < 18:
                    raise ValidationError(
                        _("You must be at least 18 years old to use Billink.")
                    )
                request.session['buckaroo_billink_birthdate'] = birthdate
                # Persist on the user's partner so the next checkout can prefill.
                user = request.env.user
                if not user._is_public():
                    user.partner_id.sudo().buckaroo_billink_birthdate = dob
        kwargs.pop('billink_tc_accepted', None)
        kwargs.pop('billink_birthdate', None)
        return super().shop_payment_transaction(order_id, access_token, **kwargs)
