/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { loadJS } from "@web/core/assets";
import { rpc } from "@web/core/network/rpc";
import { patch } from '@web/core/utils/patch';
import { PaymentForm } from '@payment/interactions/payment_form';

patch(PaymentForm.prototype, {

    async _prepareInlineForm(providerId, providerCode, paymentOptionId, paymentMethodCode, flow) {
        if (providerCode !== 'buckaroo_official' || paymentMethodCode !== 'buckaroo_creditcard') {
            await super._prepareInlineForm(...arguments);
            return;
        }

        const container = this.el.querySelector('#o_buckaroo_hosted_fields_container');
        if (!container || this._buckarooHFInitialized || this._buckarooHFInitPromise) {
            await super._prepareInlineForm(...arguments);
            return;
        }

        const isSecure = window.location.protocol === 'https:'
            || window.location.hostname === 'localhost'
            || window.location.hostname === '127.0.0.1';
        if (!isSecure) {
            await super._prepareInlineForm(...arguments);
            return;
        }

        this._buckarooHFInitPromise = this._initBuckarooHostedFields(container);
    },

    async _initBuckarooHostedFields(container) {
        try {
            const [, tokenData] = await Promise.all([
                loadJS('https://hostedfields-externalapi.prod-pci.buckaroo.io/v1/sdk'),
                rpc('/payment/buckaroo_official/hosted-fields-token', {
                    provider_id: parseInt(container.dataset.providerId),
                    payment_method_id: parseInt(container.dataset.paymentMethodId),
                }),
            ]);
            if (tokenData.error) {
                throw new Error(tokenData.error);
            }

            const sdkClient = new BuckarooHostedFieldsSdk.HFClient(tokenData.access_token);
            this._buckarooSdkClient = sdkClient;
            this._buckarooTokenExpiry = Date.now() + tokenData.expires_in * 1000;

            const lang = (document.documentElement.lang || 'en').split('_')[0].split('-')[0];
            sdkClient.setLanguage(lang);

            const brandsAttr = container.dataset.supportedBrands || '';
            const brands = [...new Set(brandsAttr ? brandsAttr.split(',').filter(Boolean) : [])];
            if (brands.length) {
                sdkClient.setSupportedServices(brands);
            }

            await sdkClient.startSession((event) => {
                sdkClient.handleValidation(
                    event,
                    'buckaroo-cardholder-name-error',
                    'buckaroo-card-number-error',
                    'buckaroo-expiry-error',
                    'buckaroo-cvc-error',
                );
            });

            const fieldStyle = {
                fontSize: '16px',
                fontFamily: 'Arial, Helvetica, sans-serif',
                textAlign: 'left',
                color: '#212529',
                background: 'transparent',
                placeholderColor: '#adb5bd',
            };

            await Promise.all([
                sdkClient.mountCardNumber('#buckaroo-card-number', {
                    id: 'ccnumber',
                    placeHolder: '1234 1234 1234 1234',
                    labelSelector: '#buckaroo-card-number-label',
                    baseStyling: fieldStyle,
                }),
                sdkClient.mountExpiryDate('#buckaroo-expiry', {
                    id: 'ccexpiry',
                    placeHolder: 'MM / YY',
                    labelSelector: '#buckaroo-expiry-label',
                    baseStyling: fieldStyle,
                }),
                sdkClient.mountCvc('#buckaroo-cvc', {
                    id: 'cccvc',
                    placeHolder: 'CVC',
                    labelSelector: '#buckaroo-cvc-label',
                    baseStyling: fieldStyle,
                }),
                sdkClient.mountCardHolderName('#buckaroo-cardholder-name', {
                    id: 'ccname',
                    placeHolder: 'J. Smith',
                    labelSelector: '#buckaroo-cardholder-name-label',
                    baseStyling: fieldStyle,
                }),
            ]);

            this._buckarooHFInitialized = true;
        } catch (error) {
            console.error('Buckaroo Hosted Fields init failed:', error);
            this._displayErrorDialog(
                _t("Error"),
                _t("Could not initialize the credit card form. Please try again."),
            );
            this._enableButton();
        }
    },

    async _initiatePaymentFlow(providerCode, paymentOptionId, paymentMethodCode, flow) {
        if (providerCode !== 'buckaroo_official' || paymentMethodCode !== 'buckaroo_creditcard') {
            await super._initiatePaymentFlow(...arguments);
            return;
        }

        const brandSelect = this.el.querySelector('#o_buckaroo_cc_brand');
        if (brandSelect && !brandSelect.value) {
            this._displayErrorDialog(_t("Error"), _t("Please select your card type."));
            this._enableButton();
            return;
        }

        if (this._buckarooHFInitPromise) {
            await this._buckarooHFInitPromise;
        }
        if (!this._buckarooHFInitialized) {
            await super._initiatePaymentFlow(...arguments);
            return;
        }

        try {
            if (Date.now() >= this._buckarooTokenExpiry) {
                this._displayErrorDialog(
                    _t("Error"),
                    _t("Your session has expired. Please refresh the page and try again."),
                );
                this._enableButton();
                return;
            }

            this._buckarooHFSessionId = await this._buckarooSdkClient.submitSession();
            this._buckarooHFService = this._buckarooSdkClient.getService() || '';
        } catch (error) {
            this._buckarooHFSessionId = null;
            this._buckarooHFService = null;
            this._displayErrorDialog(
                _t("Error"),
                _t("Payment submission failed. Please verify your card details and try again."),
            );
            this._enableButton();
            return;
        }

        await super._initiatePaymentFlow(...arguments);
    },

    _prepareTransactionRouteParams() {
        const params = super._prepareTransactionRouteParams(...arguments);
        if (this._buckarooHFSessionId) {
            params.buckaroo_hf_session_id = this._buckarooHFSessionId;
            params.buckaroo_hf_service = this._buckarooHFService;
        }
        const brandSelect = this.el.querySelector('#o_buckaroo_cc_brand');
        if (brandSelect && brandSelect.value) {
            params.buckaroo_cc_brand = brandSelect.value;
        }
        return params;
    },
});
