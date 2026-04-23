/** @odoo-module **/

import { patch } from '@web/core/utils/patch';
import { PaymentForm } from '@payment/interactions/payment_form';

patch(PaymentForm.prototype, {

    _prepareTransactionRouteParams() {
        const params = super._prepareTransactionRouteParams(...arguments);
        const radio = this.el.querySelector('input[name="o_payment_radio"]:checked');
        if (!radio || radio.dataset.providerCode !== 'buckaroo_official') {
            return params;
        }
        if (radio.dataset.paymentMethodCode === 'billink') {
            const cb = document.querySelector('.o_buckaroo_billink_tc');
            params.billink_tc_accepted = !!(cb && cb.checked);
            const birthdate = document.querySelector('.o_buckaroo_billink_birthdate');
            if (birthdate && birthdate.value) {
                params.billink_birthdate = birthdate.value;
            }
        }
        return params;
    },
});
