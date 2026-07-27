/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { patch } from '@web/core/utils/patch';
import { PaymentForm } from '@payment/interactions/payment_form';

patch(PaymentForm.prototype, {

    async _initiatePaymentFlow(providerCode, paymentOptionId, paymentMethodCode, flow) {
        // Container is rendered only when the picked Buckaroo brand is a
        // giftcard inline method, so its presence is the single source of
        // truth for the inline branch.
        const container = providerCode === 'buckaroo_official' && this.el.querySelector(
            `.o_buckaroo_giftcard_container[data-payment-method-id="${paymentOptionId}"]`
        );
        if (!container) {
            await super._initiatePaymentFlow(...arguments);
            return;
        }
        const cardnumber = container.querySelector('.o_buckaroo_giftcard_cardnumber')?.value?.trim() || '';
        const pin = container.querySelector('.o_buckaroo_giftcard_pin')?.value?.trim() || '';
        if (!cardnumber || !pin) {
            this._displayErrorDialog(
                _t("Error"),
                _t("Please enter the giftcard number and PIN."),
            );
            this._enableButton();
            return;
        }
        this._buckarooGiftcardCardnumber = cardnumber;
        this._buckarooGiftcardPin = pin;
        try {
            await super._initiatePaymentFlow(...arguments);
        } finally {
            delete this._buckarooGiftcardCardnumber;
            delete this._buckarooGiftcardPin;
        }
    },

    async selectPaymentOption(ev) {
        delete this._buckarooGiftcardCardnumber;
        delete this._buckarooGiftcardPin;
        await super.selectPaymentOption(...arguments);
    },

    _prepareTransactionRouteParams() {
        const params = super._prepareTransactionRouteParams(...arguments);
        if (this._buckarooGiftcardCardnumber) {
            params.buckaroo_giftcard_cardnumber = this._buckarooGiftcardCardnumber;
            params.buckaroo_giftcard_pin = this._buckarooGiftcardPin;
        }
        return params;
    },
});
