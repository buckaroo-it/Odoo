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
        if (radio.dataset.paymentMethodCode === 'buckaroo_paypermail') {
            const gender = document.querySelector('select.o_buckaroo_paypermail_gender');
            if (gender && gender.value) {
                params.paypermail_gender = gender.value;
            }
        }
        return params;
    },
});
