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
        if (radio.dataset.paymentMethodCode === 'in3') {
            const birthdate = document.querySelector('.o_buckaroo_in3_birthdate');
            if (birthdate && birthdate.value) {
                params.in3_birthdate = birthdate.value;
            }
        }
        return params;
    },
});
