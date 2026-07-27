/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { loadJS } from "@web/core/assets";
import { rpc } from '@web/core/network/rpc';
import { patch } from '@web/core/utils/patch';
import { registry } from '@web/core/registry';
import { PaymentForm } from '@payment/interactions/payment_form';
import { ExpressCheckout } from '@payment/interactions/express_checkout';
import {
    bindProductCart,
    displayWalletError,
    finalizeExpressTransaction,
    isDuplicatePlacement,
    snapshotProductDom,
    truthyData,
} from '@payment_buckaroo_official/js/express_wallet_utils';

const GOOGLE_PAY_JS_URL = 'https://pay.google.com/gp/p/js/pay.js';

export function buildGooglePayRequest(config, context) {
    const merchantGuid = config?.merchantGuid || '';
    if (!merchantGuid) {
        throw new Error('Missing merchantGuid for Google Pay request.');
    }
    const totalPrice = Number(context?.totalPrice);
    if (!Number.isFinite(totalPrice)) {
        throw new Error('Missing or invalid totalPrice for Google Pay request.');
    }
    const currencyCode = String(context?.currencyCode || '').toUpperCase();
    if (!currencyCode) {
        throw new Error('Missing currencyCode for Google Pay request.');
    }
    const countryCode = String(context?.countryCode || '').toUpperCase();
    if (!countryCode) {
        throw new Error('Missing countryCode for Google Pay request.');
    }
    const request = {
        apiVersion: 2,
        apiVersionMinor: 0,
        allowedPaymentMethods: [{
            type: 'CARD',
            parameters: {
                allowedAuthMethods: ['PAN_ONLY', 'CRYPTOGRAM_3DS'],
                allowedCardNetworks: ['AMEX', 'DISCOVER', 'INTERAC', 'JCB', 'MASTERCARD', 'VISA'],
                billingAddressRequired: true,
                billingAddressParameters: { format: 'FULL', phoneNumberRequired: false },
            },
            tokenizationSpecification: {
                type: 'PAYMENT_GATEWAY',
                parameters: { gateway: 'buckaroo', gatewayMerchantId: merchantGuid },
            },
        }],
        merchantInfo: {
            merchantId: config?.googleMerchantId || '',
            merchantName: config?.merchantName || 'Online Store',
        },
        transactionInfo: {
            totalPriceStatus: 'FINAL',
            totalPrice: String(totalPrice),
            currencyCode,
            countryCode,
        },
        shippingAddressRequired: Boolean(context?.shippingRequired),
        emailRequired: Boolean(context?.emailRequired),
    };
    if (context?.shippingRequired) {
        // Show the delivery-method selector in the sheet and route its changes
        // through `onPaymentDataChanged` (set on the express PaymentsClient).
        request.shippingAddressParameters = { phoneNumberRequired: false };
        request.shippingOptionRequired = true;
        request.callbackIntents = ['SHIPPING_ADDRESS', 'SHIPPING_OPTION'];
    }
    return request;
}

function _gpExtractTokenAndName(paymentData) {
    const token = paymentData?.paymentMethodData?.tokenizationData?.token || '';
    const billing = paymentData?.paymentMethodData?.info?.billingAddress || {};
    const shipping = paymentData?.shippingAddress || {};
    const customerName = (billing.name || shipping.name || '').trim();
    return { token, customerName, billing, shipping };
}

function _gpExtractAddress(address, fallbackEmail) {
    if (!address) return {};
    return {
        name: address.name || '',
        email: fallbackEmail || '',
        phone: address.phoneNumber || '',
        street: address.address1 || '',
        street2: [address.address2, address.address3].filter(Boolean).join(' '),
        zip: address.postalCode || '',
        city: address.locality || '',
        country: address.countryCode || '',
        state: address.administrativeArea || '',
    };
}

// Google's mid-sheet callback only exposes a redacted address (country, zip,
// locality, region) - enough to rate the carriers.
function _gpRedactedAddress(address) {
    if (!address) return {};
    return {
        country: address.countryCode || '',
        zip: address.postalCode || '',
        city: address.locality || '',
        state: address.administrativeArea || '',
    };
}

function _gpTransactionInfo(interaction, total) {
    return {
        countryCode: interaction._gpCountryCode || '',
        currencyCode: (interaction.paymentContext.currencyName || '').toUpperCase(),
        totalPriceStatus: 'FINAL',
        totalPrice: parseFloat(total || 0).toFixed(2),
        totalPriceLabel: _t('Total'),
    };
}

// Fires while the sheet is open: on address pick it needs the serviceable
// carriers, on method switch it needs the new total. Same Buckaroo routes the
// Apple Pay shipping callbacks use; money math stays server-side.
async function _gpOnPaymentDataChanged(interaction, intermediate) {
    if (intermediate.callbackTrigger === 'SHIPPING_OPTION') {
        let resp;
        try {
            resp = await rpc('/shop/buckaroo/wallet/set_method', {
                dm_id: intermediate.shippingOptionData?.id,
            });
        } catch (error) {
            console.error('Google Pay shipping method update failed:', error);
            return { error: {
                reason: 'OTHER_ERROR',
                message: _t("Could not update the delivery method."),
                intent: 'SHIPPING_OPTION',
            } };
        }
        return { newTransactionInfo: _gpTransactionInfo(interaction, resp.amount) };
    }
    let resp;
    try {
        resp = await rpc('/shop/buckaroo/wallet/shipping_address', {
            partial_delivery_address: _gpRedactedAddress(intermediate.shippingAddress),
        });
    } catch (error) {
        console.error('Google Pay shipping address update failed:', error);
        return { error: {
            reason: 'SHIPPING_ADDRESS_UNSERVICEABLE',
            message: _t("Could not load delivery methods for this address."),
            intent: 'SHIPPING_ADDRESS',
        } };
    }
    const methods = resp.delivery_methods || [];
    if (!methods.length) {
        return { error: {
            reason: 'SHIPPING_ADDRESS_UNSERVICEABLE',
            message: _t("No delivery method is available for this address."),
            intent: 'SHIPPING_ADDRESS',
        } };
    }
    const shippingOptions = methods.map((m) => ({
        id: String(m.id),
        label: m.name,
        description: '',
    }));
    return {
        newShippingOptionParameters: {
            // Odoo preselects the cheapest server-side; mirror that here.
            defaultSelectedOptionId: shippingOptions[0].id,
            shippingOptions,
        },
        newTransactionInfo: _gpTransactionInfo(interaction, resp.amount),
    };
}

patch(PaymentForm.prototype, {

    async _prepareInlineForm(_providerId, providerCode, _paymentOptionId, paymentMethodCode, _flow) {
        if (providerCode !== 'buckaroo_official' || paymentMethodCode !== 'buckaroo_googlepay') {
            await super._prepareInlineForm(...arguments);
            return;
        }
        const container = this.el.querySelector('#o_buckaroo_googlepay_container');
        if (!container) {
            await super._prepareInlineForm(...arguments);
            return;
        }
        if (!this._buckarooGPLoadPromise) {
            this._buckarooGPLoadPromise = loadJS(GOOGLE_PAY_JS_URL).catch((error) => {
                this._buckarooGPLoadPromise = null;
                throw error;
            });
        }
        await this._buckarooGPLoadPromise;
    },

    async _initiatePaymentFlow(providerCode, _paymentOptionId, paymentMethodCode, _flow) {
        if (providerCode !== 'buckaroo_official' || paymentMethodCode !== 'buckaroo_googlepay') {
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
        const env = container.dataset.environment === 'PRODUCTION' ? 'PRODUCTION' : 'TEST';
        if (!this._buckarooGPClient) {
            this._buckarooGPClient = new google.payments.api.PaymentsClient({ environment: env });
        }

        let paymentData;
        try {
            paymentData = await this._buckarooGPClient.loadPaymentData(buildGooglePayRequest(
                {
                    merchantGuid: container.dataset.merchantGuid || '',
                    googleMerchantId: container.dataset.googleMerchantId || '',
                    merchantName: container.dataset.merchantName || '',
                },
                {
                    totalPrice: parseFloat(container.dataset.totalAmount),
                    currencyCode: container.dataset.currencyCode || '',
                    countryCode: container.dataset.countryCode || '',
                },
            ));
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

        const { token, customerName } = _gpExtractTokenAndName(paymentData);
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
            // Single-use token; clear so a retry forces a new popup.
            this._buckarooGPToken = null;
            this._buckarooGPCustomerName = null;
        }
    },

    _prepareTransactionRouteParams() {
        const params = super._prepareTransactionRouteParams(...arguments);
        if (!this._buckarooGPToken && !this._buckarooGPCustomerName) {
            return params;
        }
        const radio = this.el.querySelector('input[name="o_payment_radio"]:checked');
        if (!radio
            || radio.dataset.providerCode !== 'buckaroo_official'
            || radio.dataset.paymentMethodCode !== 'buckaroo_googlepay') {
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

async function _gpMountButton(interaction, container, providerData) {
    const merchantGuid = container.dataset.merchantGuid || '';
    const paymentMethodId = parseInt(container.dataset.paymentMethodId);
    if (!merchantGuid || !paymentMethodId) {
        container.style.display = 'none';
        return;
    }
    if (!interaction._gpLoadPromise) {
        interaction._gpLoadPromise = loadJS(GOOGLE_PAY_JS_URL).catch((error) => {
            interaction._gpLoadPromise = null;
            throw error;
        });
    }
    await interaction._gpLoadPromise;
    if (typeof google === 'undefined' || !google.payments?.api) {
        container.style.display = 'none';
        return;
    }

    const env = container.dataset.environment === 'PRODUCTION' ? 'PRODUCTION' : 'TEST';
    // Cart-page deliverable carts get the in-sheet shipping selector; the
    // product page has no cart yet, so it keeps the address-only flow.
    const shippingRequired = container.dataset.placement !== 'product'
        && !!interaction.paymentContext.shippingInfoRequired;
    interaction._gpCountryCode = container.dataset.countryCode || '';
    if (!interaction._gpClient) {
        const clientOptions = { environment: env };
        if (shippingRequired) {
            // Required whenever the request sets callbackIntents, else Google throws.
            clientOptions.paymentDataCallbacks = {
                onPaymentDataChanged: (intermediate) =>
                    _gpOnPaymentDataChanged(interaction, intermediate),
            };
        }
        interaction._gpClient = new google.payments.api.PaymentsClient(clientOptions);
    }
    const paymentsClient = interaction._gpClient;
    const config = {
        merchantGuid,
        googleMerchantId: container.dataset.googleMerchantId || '',
        merchantName: container.dataset.merchantName || '',
    };
    const buildRequest = (overrides = {}) => buildGooglePayRequest(config, {
        totalPrice: parseFloat(interaction.paymentContext.amount || 0),
        currencyCode: (interaction.paymentContext.currencyName || '').toUpperCase(),
        countryCode: container.dataset.countryCode || '',
        shippingRequired: !!interaction.paymentContext.shippingInfoRequired,
        emailRequired: true,
        ...overrides,
    });

    let isReady = false;
    try {
        const ready = await paymentsClient.isReadyToPay({
            apiVersion: 2,
            apiVersionMinor: 0,
            allowedPaymentMethods: buildRequest().allowedPaymentMethods,
        });
        isReady = !!ready?.result;
    } catch (error) {
        console.error('Google Pay isReadyToPay failed:', error);
    }
    if (!isReady) {
        container.style.display = 'none';
        return;
    }

    const isProduct = container.dataset.placement === 'product';
    const button = paymentsClient.createButton({
        onClick: () => _gpExpressClick(
            interaction, paymentsClient, buildRequest, providerData, paymentMethodId, isProduct,
        ),
        buttonType: 'buy',
        buttonSizeMode: 'fill',
        // 8px radius matches the shared wallet-button geometry (wallet_buttons.css);
        // the container's 40px height drives the filled button height.
        buttonRadius: 8,
        buttonColor: container.dataset.buttonStyle === 'white' ? 'white' : 'black',
    });
    container.replaceChildren(button);
}

async function _gpExpressClick(interaction, paymentsClient, buildRequest, providerData, paymentMethodId, isProduct) {
    // `loadPaymentData` must run inside the user-gesture chain - any
    // await before it breaks Safari and Google Pay blocks the popup.
    let snapshot = null;
    let request;
    if (isProduct) {
        snapshot = snapshotProductDom();
        if (!snapshot) {
            displayWalletError(interaction, _t(
                "Could not read the product details. Please reload the page and try again.",
            ));
            return;
        }
        request = buildRequest({ totalPrice: snapshot.unitPrice * snapshot.qty, shippingRequired: false });
    } else {
        request = buildRequest();
    }

    let paymentData;
    try {
        paymentData = await paymentsClient.loadPaymentData(request);
    } catch (error) {
        if (error?.statusCode !== 'CANCELED') {
            console.error('Google Pay loadPaymentData failed:', error);
            displayWalletError(interaction, _t(
                "Google Pay payment could not be processed. Please try again.",
            ));
        }
        return;
    }

    // Bind the variant to a cart only after the popup returns: the
    // finalize step needs a transaction route and partner id.
    if (isProduct && !(await bindProductCart(
        interaction,
        '/shop/buckaroo/googlepay/express_init',
        snapshot,
        _t("Google Pay could not start. Please try again."),
    ))) {
        return;
    }

    await _gpFinalize(interaction, paymentData, providerData, paymentMethodId);
}

async function _gpFinalize(interaction, paymentData, providerData, paymentMethodId) {
    const { token, customerName, billing, shipping } = _gpExtractTokenAndName(paymentData);
    if (!token || !customerName) {
        displayWalletError(interaction, _t(
            "Google Pay returned an incomplete response. Please try again.",
        ));
        return;
    }

    const email = paymentData?.email || '';
    const addresses = { billing_address: _gpExtractAddress(billing, email) };
    if (interaction.paymentContext.shippingInfoRequired && shipping?.name) {
        addresses.shipping_address = _gpExtractAddress(shipping, email);
    }
    const shipOptId = paymentData?.shippingOptionData?.id;
    if (shipOptId) {
        // Re-apply the picked carrier so its cost is in the order total Buckaroo charges.
        addresses.shipping_option = { id: String(shipOptId) };
    }
    await finalizeExpressTransaction(interaction, {
        providerId: providerData.providerId,
        paymentMethodId,
        addresses,
        txExtra: {
            buckaroo_googlepay_token: token,
            buckaroo_googlepay_customer_name: customerName,
        },
        txError: _t("Google Pay payment could not be processed. Please try again."),
    });
}

patch(ExpressCheckout.prototype, {
    async _prepareExpressCheckoutForm(providerData) {
        if (providerData.providerCode !== 'buckaroo_official') {
            await super._prepareExpressCheckoutForm(...arguments);
            return;
        }
        const container = this.el.querySelector('div[name="o_express_checkout_container"]');
        if (!container) {
            await super._prepareExpressCheckoutForm(...arguments);
            return;
        }
        // Keep only the first cart button when a theme renders the express form twice.
        if (isDuplicatePlacement(
            container,
            'form[name="o_payment_express_checkout_form"] div[name="o_express_checkout_container"][data-merchant-guid]',
        )) {
            container.style.display = 'none';
            return;
        }
        await _gpMountButton(this, container, providerData);
    },
});

// Product page lives inside Odoo's Add-to-Cart `<form>`. HTML5
// forbids nested forms, so we bind a standalone container instead
// of reusing the base form selector.
export class BuckarooGooglePayProductExpress extends ExpressCheckout {
    static selector = 'div[name="o_express_checkout_container"][data-placement="product"]';

    setup() {
        this.paymentContext = { ...this.el.dataset };
        this.paymentContext.shippingInfoRequired = truthyData(this.paymentContext.shippingInfoRequired);
    }

    async willStart() {
        await _gpMountButton(this, this.el, {
            providerId: parseInt(this.el.dataset.providerId),
            providerCode: this.el.dataset.providerCode,
        });
    }

    start() {
        // No `cart_amount_changed` listener: product page has no cart.
    }
}

registry
    .category('public.interactions')
    .add('payment_buckaroo_official.googlepay_product_express', BuckarooGooglePayProductExpress);
