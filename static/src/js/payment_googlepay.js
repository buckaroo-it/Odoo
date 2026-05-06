/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { loadJS } from "@web/core/assets";
import { ConfirmationDialog } from '@web/core/confirmation_dialog/confirmation_dialog';
import { rpc } from '@web/core/network/rpc';
import { redirect } from '@web/core/utils/urls';
import { patch } from '@web/core/utils/patch';
import { registry } from '@web/core/registry';
import { PaymentForm } from '@payment/interactions/payment_form';
import { ExpressCheckout } from '@payment/interactions/express_checkout';

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
    return {
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

function _gpDisplayError(interaction, message) {
    interaction.services.dialog.add(ConfirmationDialog, {
        title: _t("Error"),
        body: message,
    });
}

// Variant changes are not tracked via events; upstream `view_item_event`
// is GA-gated. Read fresh DOM at click time instead.
function _gpSnapshotProductDom() {
    const main = document.querySelector('.js_main_product')
        || document.querySelector('#product_detail');
    if (!main) return null;
    const productInput = main.querySelector('input.product_id, input[name="product_id"]');
    const qtyInput = main.querySelector('input[name="add_qty"]');
    const priceEl = main.querySelector('.product_price .oe_currency_value');
    const productId = parseInt(productInput?.value || '0');
    const qty = parseInt(qtyInput?.value || '1') || 1;
    const rawPrice = (priceEl?.textContent || '').trim().replace(/\s+/g, '').replace(/,/g, '.');
    const unitPrice = parseFloat(rawPrice);
    if (!Number.isFinite(productId) || productId <= 0) return null;
    if (!Number.isFinite(unitPrice) || unitPrice <= 0) return null;
    return { productId, qty: Math.max(qty, 1), unitPrice };
}

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
            this._buckarooGPLoadPromise = loadJS(GOOGLE_PAY_JS_URL).catch((error) => {
                this._buckarooGPLoadPromise = null;
                throw error;
            });
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
            || radio.dataset.paymentMethodCode !== 'googlepay') {
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
    if (!interaction._gpClient) {
        interaction._gpClient = new google.payments.api.PaymentsClient({ environment: env });
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
        snapshot = _gpSnapshotProductDom();
        if (!snapshot) {
            _gpDisplayError(interaction, _t(
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
            _gpDisplayError(interaction, _t(
                "Google Pay payment could not be processed. Please try again.",
            ));
        }
        return;
    }

    // Bind the variant to a cart only after the popup returns: the
    // finalize step needs a transaction route and partner id.
    if (isProduct) {
        let payload;
        try {
            payload = await rpc('/shop/buckaroo/googlepay/express_init', {
                product_id: snapshot.productId,
                qty: snapshot.qty,
            });
        } catch (error) {
            console.error('Google Pay express init failed:', error);
            _gpDisplayError(interaction, _t("Google Pay could not start. Please try again."));
            return;
        }
        Object.assign(interaction.paymentContext, {
            amount: payload.amount,
            minorAmount: payload.minor_amount,
            currencyName: String(payload.currency_code || '').toUpperCase(),
            partnerId: payload.partner_id,
            transactionRoute: payload.transaction_route,
            expressCheckoutRoute: payload.express_checkout_route,
            shippingInfoRequired: !!payload.shipping_info_required,
            accessToken: payload.access_token,
            landingRoute: payload.landing_route || '/shop/payment/validate',
        });
    }

    await _gpFinalize(interaction, paymentData, providerData, paymentMethodId);
}

async function _gpFinalize(interaction, paymentData, providerData, paymentMethodId) {
    const { token, customerName, billing, shipping } = _gpExtractTokenAndName(paymentData);
    if (!token || !customerName) {
        _gpDisplayError(interaction, _t(
            "Google Pay returned an incomplete response. Please try again.",
        ));
        return;
    }

    const email = paymentData?.email || '';
    const addresses = { billing_address: _gpExtractAddress(billing, email) };
    if (interaction.paymentContext.shippingInfoRequired && shipping?.name) {
        addresses.shipping_address = _gpExtractAddress(shipping, email);
    }

    let partnerId;
    try {
        partnerId = await interaction.waitFor(rpc(
            interaction.paymentContext.expressCheckoutRoute, addresses,
        ));
    } catch (error) {
        console.error('Google Pay address update RPC failed:', error);
        _gpDisplayError(interaction, _t("Address update failed. Please try again."));
        return;
    }
    const parsedPartnerId = parseInt(partnerId);
    if (!Number.isFinite(parsedPartnerId)) {
        console.error('Google Pay address update returned invalid partner id:', partnerId);
        _gpDisplayError(interaction, _t("Address update failed. Please try again."));
        return;
    }
    interaction.paymentContext.partnerId = parsedPartnerId;

    let processingValues;
    try {
        processingValues = await interaction.waitFor(rpc(
            interaction.paymentContext.transactionRoute,
            {
                provider_id: parseInt(providerData.providerId),
                // Override framework default of payment_method_unknown so
                // backend dispatches to the googlepay branch.
                payment_method_id: paymentMethodId,
                token_id: null,
                flow: 'direct',
                tokenization_requested: false,
                landing_route: interaction.paymentContext.landingRoute,
                access_token: interaction.paymentContext.accessToken,
                csrf_token: odoo.csrf_token,
                buckaroo_googlepay_token: token,
                buckaroo_googlepay_customer_name: customerName,
            },
        ));
    } catch (error) {
        console.error('Google Pay transaction RPC failed:', error);
        _gpDisplayError(interaction, _t(
            "Google Pay payment could not be processed. Please try again.",
        ));
        return;
    }

    // Buckaroo redirect-based flow: tx returns api_url to its hosted
    // page. Cross-origin so use `window.location.assign` (Odoo's
    // `redirect()` blocks foreign origins).
    if (processingValues?.api_url) {
        window.location.assign(processingValues.api_url);
        return;
    }
    redirect('/payment/status');
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
        // Some themes render multiple express placements per page
        // (cart sidebar + shorter_cart_summary); keep only the first.
        const all = document.querySelectorAll(
            'form[name="o_payment_express_checkout_form"] div[name="o_express_checkout_container"][data-merchant-guid]'
        );
        if (all.length > 1 && all[0] !== container) {
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
        // dataset values are strings; "False" is truthy via `!!`.
        this.paymentContext.shippingInfoRequired = (
            this.paymentContext.shippingInfoRequired === 'True'
            || this.paymentContext.shippingInfoRequired === 'true'
        );
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
