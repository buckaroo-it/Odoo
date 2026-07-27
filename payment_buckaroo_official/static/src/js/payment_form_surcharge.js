import { patch } from '@web/core/utils/patch';
import { rpc, ConnectionAbortedError } from '@web/core/network/rpc';
import { PaymentForm } from '@payment/interactions/payment_form';

patch(PaymentForm.prototype, {

    async selectPaymentOption(ev) {
        await super.selectPaymentOption(ev);

        const radio = ev.target;
        const optionId = parseInt(radio.dataset.paymentOptionId);
        if (!optionId) {
            return;
        }
        const payload = {};
        if (radio.dataset.paymentOptionType === 'payment_method') {
            payload.payment_method_id = optionId;
        } else if (radio.dataset.paymentOptionType === 'token') {
            payload.token_id = optionId;
        } else {
            return;
        }

        // Cancel in-flight rpc; only latest selection lands.
        if (this._buckarooSetMethodReq) {
            this._buckarooSetMethodReq.abort(false);
        }
        // Block submit; data-amount stale until rpc settles.
        this._disableButton();

        const req = rpc('/payment/buckaroo_official/set_method', payload);
        this._buckarooSetMethodReq = req;
        let result;
        try {
            result = await req;
        } catch (error) {
            if (error instanceof ConnectionAbortedError) {
                return;
            }
            this._buckarooNotifyError(error);
            return;
        } finally {
            if (this._buckarooSetMethodReq === req) {
                this._buckarooSetMethodReq = null;
                this._enableButton(false);
            }
        }
        if (!result || !result.amount_total) {
            return;
        }
        this._buckarooUpdateCartSummaries(result);
    },

    _buckarooNotifyError(error) {
        const message = error?.data?.message || error?.message;
        const notification = this.services?.notification;
        if (notification && message) {
            notification.add(message, { type: 'danger' });
        } else {
            console.error('[payment_buckaroo_official] set_method failed:', error);
        }
    },

    _buckarooUpdateCartSummaries(result) {
        const parents = document.querySelectorAll(
            '#o_cart_summary_offcanvas, div.o_total_card',
        );
        parents.forEach(parent => {
            const untaxed = parent.querySelector(
                'tr[name="o_order_total_untaxed"] .monetary_field',
            );
            const tax = parent.querySelector(
                'tr[name="o_order_total_taxes"] .monetary_field',
            );
            const totals = (parent.parentElement || parent).querySelectorAll(
                'tr[name="o_order_total"] .monetary_field, '
                + '#amount_total_summary.monetary_field',
            );
            if (untaxed) untaxed.textContent = result.amount_untaxed;
            if (tax) tax.textContent = result.amount_tax;
            totals.forEach(t => t.textContent = result.amount_total);

            const surchargeRow = parent.querySelector(
                'tr[name="o_order_buckaroo_surcharge"]',
            );
            if (surchargeRow) {
                const surchargeAmount = surchargeRow.querySelector('.monetary_field');
                if (surchargeAmount) {
                    surchargeAmount.textContent = result.amount_buckaroo_surcharge;
                }
                surchargeRow.classList.toggle('d-none', !result.has_buckaroo_surcharge);
            }
        });
    },

});
