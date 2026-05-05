/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { loadJS } from "@web/core/assets";
import { patch } from '@web/core/utils/patch';
import { PaymentForm } from '@payment/interactions/payment_form';

const GOOGLE_PAY_JS_URL = 'https://pay.google.com/gp/p/js/pay.js';
const ALLOWED_AUTH_METHODS = ['PAN_ONLY', 'CRYPTOGRAM_3DS'];
const ALLOWED_CARD_NETWORKS = ['AMEX', 'DISCOVER', 'INTERAC', 'JCB', 'MASTERCARD', 'VISA'];

patch(PaymentForm.prototype, {

    async _prepareInlineForm(_providerId, providerCode, _paymentOptionId, paymentMethodCode, _flow) {
        if (providerCode !== 'buckaroo_official' || paymentMethodCode !== 'googlepay') {
            await super._prepareInlineForm(...arguments);
            return;
        }
        const container = this.el.querySelector('#o_buckaroo_googlepay_container');
        if (!container) {
            await super._prepareInlineForm(...arguments);
            return;
        }
        if (!this._buckarooGPLoadPromise) {
            this._buckarooGPLoadPromise = loadJS(GOOGLE_PAY_JS_URL);
        }
        await this._buckarooGPLoadPromise;
    },

    async _initiatePaymentFlow(providerCode, _paymentOptionId, paymentMethodCode, _flow) {
        if (providerCode !== 'buckaroo_official' || paymentMethodCode !== 'googlepay') {
            return super._initiatePaymentFlow(...arguments);
        }
        const container = this.el.querySelector('#o_buckaroo_googlepay_container');
        if (!container || typeof google === 'undefined' || !google.payments?.api) {
            this._displayErrorDialog(
                _t("Error"),
                _t("Google Pay is not ready. Please reload the page and try again."),
            );
            this._enableButton();
            return;
        }

        let paymentData;
        try {
            paymentData = await this._buckarooGPRequestPaymentData(container);
        } catch (error) {
            if (error?.statusCode !== 'CANCELED') {
                console.error('Google Pay loadPaymentData failed:', error);
                this._displayErrorDialog(
                    _t("Error"),
                    _t("Google Pay payment could not be processed. Please try again."),
                );
            }
            this._enableButton();
            return;
        }

        const token = paymentData?.paymentMethodData?.tokenizationData?.token || '';
        const billing = paymentData?.paymentMethodData?.info?.billingAddress || {};
        const shipping = paymentData?.shippingAddress || {};
        const customerName = (billing.name || shipping.name || '').trim();
        if (!token || !customerName) {
            this._displayErrorDialog(
                _t("Error"),
                _t("Google Pay returned an incomplete response. Please try again."),
            );
            this._enableButton();
            return;
        }

        this._buckarooGPToken = token;
        this._buckarooGPCustomerName = customerName;
        try {
            await super._initiatePaymentFlow(...arguments);
        } finally {
            // Google tokens are single-use; clear so a retry forces a new popup.
            this._buckarooGPToken = null;
            this._buckarooGPCustomerName = null;
        }
    },

    async _buckarooGPRequestPaymentData(container) {
        const env = container.dataset.environment === 'PRODUCTION' ? 'PRODUCTION' : 'TEST';
        if (!this._buckarooGPClient) {
            this._buckarooGPClient = new google.payments.api.PaymentsClient({ environment: env });
        }
        const paymentsClient = this._buckarooGPClient;
        const totalPrice = parseFloat(container.dataset.totalAmount);
        if (!Number.isFinite(totalPrice)) {
            throw new Error('Missing or invalid total amount on Google Pay container.');
        }
        const currencyCode = (container.dataset.currencyCode || '').toUpperCase();
        if (!currencyCode) {
            throw new Error('Missing currency code on Google Pay container.');
        }
        const countryCode = (container.dataset.countryCode || '').toUpperCase();
        if (!countryCode) {
            throw new Error('Missing country code on Google Pay container.');
        }
        const merchantGuid = container.dataset.merchantGuid || '';
        const googleMerchantId = container.dataset.googleMerchantId || '';
        const merchantName = container.dataset.merchantName || 'Online Store';

        const paymentDataRequest = {
            apiVersion: 2,
            apiVersionMinor: 0,
            allowedPaymentMethods: [{
                type: 'CARD',
                parameters: {
                    allowedAuthMethods: ALLOWED_AUTH_METHODS,
                    allowedCardNetworks: ALLOWED_CARD_NETWORKS,
                    billingAddressRequired: true,
                    billingAddressParameters: { format: 'FULL', phoneNumberRequired: false },
                },
                tokenizationSpecification: {
                    type: 'PAYMENT_GATEWAY',
                    parameters: { gateway: 'buckaroo', gatewayMerchantId: merchantGuid },
                },
            }],
            merchantInfo: {
                merchantId: googleMerchantId,
                merchantName,
            },
            transactionInfo: {
                totalPriceStatus: 'FINAL',
                totalPrice: String(totalPrice),
                currencyCode,
                countryCode,
            },
            emailRequired: false,
        };
        return paymentsClient.loadPaymentData(paymentDataRequest);
    },

    _prepareTransactionRouteParams() {
        const params = super._prepareTransactionRouteParams(...arguments);
        const radio = this.el.querySelector('input[name="o_payment_radio"]:checked');
        if (!radio || radio.dataset.providerCode !== 'buckaroo_official') {
            return params;
        }
        if (radio.dataset.paymentMethodCode !== 'googlepay') {
            return params;
        }
        if (this._buckarooGPToken) {
            params.buckaroo_googlepay_token = this._buckarooGPToken;
        }
        if (this._buckarooGPCustomerName) {
            params.buckaroo_googlepay_customer_name = this._buckarooGPCustomerName;
        }
        return params;
    },
});
