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
        if (radio.dataset.paymentMethodCode === 'buckaroo_riverty') {
            const birthdate = document.querySelector('.o_buckaroo_riverty_birthdate');
            if (birthdate && birthdate.value) {
                params.riverty_birthdate = birthdate.value;
            }
            const salutation = document.querySelector('.o_buckaroo_riverty_salutation');
            if (salutation && salutation.value) {
                params.riverty_salutation = salutation.value;
            }
            const identificationNumber = document.querySelector(
                '.o_buckaroo_riverty_identification_number'
            );
            if (identificationNumber && identificationNumber.value) {
                params.riverty_identification_number = identificationNumber.value;
            }
        }
        return params;
    },
});
