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

// Apple's SDK polyfills `<apple-pay-button>` and `applePayCapabilities` on
// non-Safari browsers; Buckaroo's SDK owns merchant validation + capture.
const APPLE_PAY_SDK_URL = 'https://applepay.cdn-apple.com/jsapi/1.latest/apple-pay-sdk.js';
const BUCKAROO_SDK_URL = 'https://checkout.buckaroo.nl/api/buckaroosdk/script';

let _apSdkPromise = null;

// Both SDKs are needed on every Apple Pay surface; memoize the load so the
// inline form and the express buttons share one fetch.
function _apEnsureSdks() {
    if (!_apSdkPromise) {
        _apSdkPromise = Promise.all([
            loadJS(APPLE_PAY_SDK_URL),
            loadJS(BUCKAROO_SDK_URL),
        ]).catch((error) => {
            _apSdkPromise = null;
            throw error;
        });
    }
    return _apSdkPromise;
}

// Shape Buckaroo expects as the PaymentData parameter (matches the Magento2
// reference). The controller and model base64-encode this string as-is.
function _apFormatToken(payment) {
    const paymentData = payment?.token?.paymentData;
    if (!paymentData?.data || !paymentData?.header) {
        return null;
    }
    return JSON.stringify({
        paymentData: {
            version: paymentData.version,
            data: paymentData.data,
            signature: paymentData.signature,
            header: {
                ephemeralPublicKey: paymentData.header.ephemeralPublicKey,
                publicKeyHash: paymentData.header.publicKeyHash,
                transactionId: paymentData.header.transactionId,
            },
        },
    });
}

function _apCustomerName(payment) {
    const contact = payment?.billingContact || {};
    return [contact.givenName, contact.familyName].filter(Boolean).join(' ').trim();
}

// Apple `ApplePayPaymentContact` -> the address shape the express checkout
// route consumes (same keys Google Pay maps to).
function _apContactToAddress(contact) {
    if (!contact) return {};
    const lines = contact.addressLines || [];
    const name = [contact.givenName, contact.familyName].filter(Boolean).join(' ').trim();
    return {
        name: name || '',
        email: contact.emailAddress || '',
        phone: contact.phoneNumber || '',
        street: lines[0] || '',
        street2: lines.slice(1).filter(Boolean).join(' '),
        zip: contact.postalCode || '',
        city: contact.locality || '',
        country: (contact.countryCode || '').toUpperCase(),
        state: contact.administrativeArea || '',
    };
}

// Odoo express delivery method -> Apple `ApplePayShippingMethod`.
function _apShippingMethod(m) {
    return {
        label: m.name,
        detail: '',
        amount: parseFloat(m.amount || 0).toFixed(2),
        identifier: String(m.id),
    };
}

function _apTotalLineItem(storeName, amount) {
    return {
        label: storeName,
        amount: parseFloat(amount || 0).toFixed(2),
        type: 'final',
    };
}

// `onShippingMethodSelected` callback: persist the picked carrier server-side
// (set_method) and return the refreshed total; on failure fall back to
// `fallbackAmount`. `onPick` runs first for callers that also track the
// selection locally.
function _apSetMethodHandler(storeName, fallbackAmount, onPick) {
    return async (method) => {
        if (onPick) {
            onPick(method);
        }
        let resp;
        try {
            resp = await rpc('/shop/buckaroo/wallet/set_method', { dm_id: method.identifier });
        } catch (error) {
            console.error('Apple Pay shipping method update failed:', error);
            return { newTotal: _apTotalLineItem(storeName, fallbackAmount) };
        }
        return { newTotal: _apTotalLineItem(storeName, resp.amount) };
    };
}

// `ApplePayError` is defined by Apple's SDK; the non-Safari polyfill may omit
// it, so degrade to no error rather than throw.
function _apError(code) {
    if (typeof ApplePayError !== 'undefined') {
        try {
            return [new ApplePayError(code)];
        } catch {
            return [];
        }
    }
    return [];
}

// Cross-browser capability gate. `applePayCapabilities` is the modern API
// available on Chrome/Firefox/Edge via Apple's SDK; the legacy Safari-only
// `checkApplePaySupport` is deliberately not used.
async function _apIsSupported(guid) {
    if (typeof ApplePaySession === 'undefined' || !window.ApplePaySession?.applePayCapabilities) {
        return false;
    }
    try {
        const caps = await ApplePaySession.applePayCapabilities(guid);
        return caps?.paymentCredentialStatus !== 'applePayUnsupported';
    } catch (error) {
        console.error('Apple Pay applePayCapabilities failed:', error);
        return false;
    }
}

// Apple's SDK defines `<apple-pay-button>` via an async `import()` with no
// ready event, so on non-Safari the element can still be undefined right after
// the SDK script runs. Wait for the registration (with a timeout guard so it
// can't hang) before inserting, or the tag renders inert. Safari ships the
// element natively, where `whenDefined` resolves immediately.
async function _apButtonElementReady() {
    if (typeof customElements === 'undefined' || !customElements.whenDefined) {
        return true;
    }
    try {
        await Promise.race([
            customElements.whenDefined('apple-pay-button'),
            new Promise((_, reject) =>
                setTimeout(() => reject(new Error('timeout')), 3000)),
        ]);
        return true;
    } catch (error) {
        console.error('apple-pay-button custom element not defined:', error);
        return false;
    }
}

// Render the native `<apple-pay-button>` into `container` after the
// capability gate, wiring `onClick`. Returns whether a button is present.
async function _apRenderButton(container, onClick) {
    const guid = container.dataset.merchantGuid || '';
    if (!guid) {
        return false;
    }
    if (container.querySelector('apple-pay-button')) {
        return true;
    }
    if (!(await _apIsSupported(guid))) {
        container.classList.add('d-none');
        return false;
    }
    if (!(await _apButtonElementReady())) {
        container.classList.add('d-none');
        return false;
    }
    const button = document.createElement('apple-pay-button');
    button.setAttribute('buttonstyle', container.dataset.buttonStyle === 'white' ? 'white' : 'black');
    button.setAttribute('type', 'buy');
    // Geometry (width/height/border-radius) comes from the shared
    // o_buckaroo_wallet_button class in wallet_buttons.css.
    button.addEventListener('click', onClick);
    container.replaceChildren(button);
    container.classList.remove('d-none');
    return true;
}

// Create the ApplePaySession (via the Buckaroo SDK) and resolve with the
// authorized token + contacts. The Promise executor runs synchronously, so
// `beginPayment` stays inside the click gesture; any await before it would
// make Safari reject the session.
function _apAuthorize(buttonSelector, params) {
    return new Promise((resolve, reject) => {
        let settled = false;
        const resolveOnce = (value) => {
            if (!settled) {
                settled = true;
                resolve(value);
            }
        };
        const storeName = params.storeName || 'Online Store';
        const options = new BuckarooSdk.ApplePay.ApplePayOptions(
            storeName,
            (params.countryCode || '').toUpperCase(),
            (params.currencyCode || '').toUpperCase(),
            params.cultureCode || 'en-US',
            params.merchantGuid || '',
            [],
            {
                label: storeName,
                amount: parseFloat(params.amount || '0').toFixed(2),
                type: 'final',
            },
            'shipping',
            params.shippingMethods || [],
            (payment) => {
                resolveOnce({
                    token: _apFormatToken(payment),
                    customerName: _apCustomerName(payment),
                    billingContact: payment?.billingContact || null,
                    shippingContact: payment?.shippingContact || null,
                });
                return Promise.resolve({ status: ApplePaySession.STATUS_SUCCESS, errors: [] });
            },
            params.onShippingMethodSelected || null,
            params.onShippingContactSelected || null,
            params.requiredBillingContactFields || ['postalAddress', 'name'],
            params.requiredShippingContactFields || [],
            () => resolveOnce({ token: null, customerName: null }),
        );
        try {
            const payment = new BuckarooSdk.ApplePay.ApplePayPayment(buttonSelector, options);
            payment.beginPayment({ preventDefault() {}, stopPropagation() {} });
        } catch (error) {
            reject(error);
        }
    });
}

patch(PaymentForm.prototype, {

    async _prepareInlineForm(_providerId, providerCode, _paymentOptionId, paymentMethodCode, _flow) {
        if (providerCode !== 'buckaroo_official' || paymentMethodCode !== 'buckaroo_applepay') {
            await super._prepareInlineForm(...arguments);
            return;
        }
        const container = this.el.querySelector('#o_buckaroo_applepay_container');
        if (!container || container.dataset.integrationMode === 'redirect') {
            // Redirect mode: skip the SDK/button and let the method submit
            // through Odoo's standard hosted-redirect flow.
            await super._prepareInlineForm(...arguments);
            return;
        }
        await _apEnsureSdks();
        // Pre-fetch the order's carriers (page load, off the gesture) so the
        // sheet can show the method selector the instant it opens - the order
        // already has the address from checkout, so no contact callback fires.
        try {
            this._buckarooAPMethods = await rpc('/shop/buckaroo/wallet/methods', {});
        } catch (error) {
            console.error('Apple Pay shipping methods prefetch failed:', error);
            this._buckarooAPMethods = null;
        }
        // Drive the standard submit path so the transaction route + redirect
        // flow are reused; the gesture is preserved synchronously into
        // `_initiatePaymentFlow`, where the ApplePaySession is created.
        await _apRenderButton(container, (ev) => this.submitForm(ev));
    },

    async _initiatePaymentFlow(providerCode, _paymentOptionId, paymentMethodCode, _flow) {
        if (providerCode !== 'buckaroo_official' || paymentMethodCode !== 'buckaroo_applepay') {
            return super._initiatePaymentFlow(...arguments);
        }
        const container = this.el.querySelector('#o_buckaroo_applepay_container');
        if (container?.dataset.integrationMode === 'redirect') {
            // Redirect mode: defer to the standard hosted-redirect flow.
            return super._initiatePaymentFlow(...arguments);
        }
        if (!container || typeof ApplePaySession === 'undefined'
                || typeof BuckarooSdk === 'undefined' || !BuckarooSdk.ApplePay) {
            this._displayErrorDialog(
                _t("Error"),
                _t("Apple Pay is not available on this browser. Please choose another payment method."),
            );
            this._enableButton();
            return;
        }

        // Seed the sheet with the order's carriers, current one first so Apple's
        // preselection matches the displayed total. Switching one persists it on
        // the order (set_method) and refreshes the total - just like Woo.
        const storeName = container.dataset.storeName || 'Online Store';
        const prefetch = this._buckarooAPMethods || {};
        const prefetchMethods = (prefetch.delivery_methods || []).slice().sort((a, b) =>
            (a.id === prefetch.selected_id ? -1 : 0) - (b.id === prefetch.selected_id ? -1 : 0));
        const initialMethods = prefetchMethods.map(_apShippingMethod);
        const shippingCallbacks = initialMethods.length ? {
            onShippingMethodSelected: _apSetMethodHandler(storeName, container.dataset.totalAmount),
        } : {};

        let token, customerName;
        try {
            ({ token, customerName } = await _apAuthorize('#o_buckaroo_applepay_container', {
                storeName,
                countryCode: container.dataset.countryCode,
                currencyCode: container.dataset.currencyCode,
                cultureCode: document.documentElement.lang,
                merchantGuid: container.dataset.merchantGuid,
                amount: container.dataset.totalAmount,
                requiredBillingContactFields: ['postalAddress', 'name'],
                requiredShippingContactFields: [],
                shippingMethods: initialMethods,
                ...shippingCallbacks,
            }));
        } catch (error) {
            console.error('Apple Pay authorization failed:', error);
            this._displayErrorDialog(
                _t("Error"),
                _t("Apple Pay payment could not be processed. Please try again."),
            );
            this._enableButton();
            return;
        }
        if (!token || !customerName) {
            // User cancelled the sheet or Apple returned an incomplete response.
            this._enableButton();
            return;
        }

        this._buckarooAPToken = token;
        this._buckarooAPCustomerName = customerName;
        try {
            await super._initiatePaymentFlow(...arguments);
        } finally {
            // Single-use token; clear so a retry forces a fresh Apple Pay sheet.
            this._buckarooAPToken = null;
            this._buckarooAPCustomerName = null;
        }
    },

    _prepareTransactionRouteParams() {
        const params = super._prepareTransactionRouteParams(...arguments);
        if (!this._buckarooAPToken && !this._buckarooAPCustomerName) {
            return params;
        }
        const radio = this.el.querySelector('input[name="o_payment_radio"]:checked');
        if (!radio
            || radio.dataset.providerCode !== 'buckaroo_official'
            || radio.dataset.paymentMethodCode !== 'buckaroo_applepay') {
            return params;
        }
        if (this._buckarooAPToken) {
            params.buckaroo_applepay_token = this._buckarooAPToken;
        }
        if (this._buckarooAPCustomerName) {
            params.buckaroo_applepay_customer_name = this._buckarooAPCustomerName;
        }
        return params;
    },
});

// Rate the shown product's carriers off a throwaway order so the product-page
// sheet can seed its method selector (no cart exists yet). Re-run on
// variant/qty changes so free-over thresholds stay accurate.
async function _apPrefetchProductMethods(interaction) {
    const snap = snapshotProductDom();
    if (!snap) {
        interaction._apProductShipping = null;
        return;
    }
    try {
        interaction._apProductShipping = await rpc('/shop/buckaroo/wallet/product_methods', {
            product_id: snap.productId,
            qty: snap.qty,
        });
    } catch (error) {
        console.error('Apple Pay product shipping prefetch failed:', error);
        interaction._apProductShipping = null;
    }
}

async function _apExpressMountButton(interaction) {
    const container = interaction.el;
    const guid = container.dataset.merchantGuid || '';
    const paymentMethodId = parseInt(container.dataset.paymentMethodId);
    if (!guid || !paymentMethodId || !container.id) {
        container.classList.add('d-none');
        return;
    }
    // Keep only the first cart button when a theme renders the express form twice.
    if (container.dataset.placement !== 'product' && isDuplicatePlacement(
        container,
        'form[name="o_payment_express_checkout_form"] div[name="o_buckaroo_applepay_express_container"][data-merchant-guid]',
    )) {
        container.classList.add('d-none');
        return;
    }
    try {
        await _apEnsureSdks();
    } catch (error) {
        console.error('Apple Pay SDK load failed:', error);
        container.classList.add('d-none');
        return;
    }
    if (typeof BuckarooSdk === 'undefined' || !BuckarooSdk.ApplePay) {
        container.classList.add('d-none');
        return;
    }
    interaction._apMounted = await _apRenderButton(
        container,
        () => _apExpressClick(interaction, container, paymentMethodId),
    );
    if (interaction._apMounted && container.dataset.placement === 'product') {
        await _apPrefetchProductMethods(interaction);
    }
}

async function _apExpressClick(interaction, container, paymentMethodId) {
    // `_apAuthorize` creates the ApplePaySession synchronously - no await
    // before it or Safari rejects the session.
    const isProduct = container.dataset.placement === 'product';
    const storeName = container.dataset.storeName || 'Online Store';
    let snapshot = null;
    let amount;
    let currencyCode;
    let initialMethods = [];
    let requiredShippingContactFields = [];
    // Selected while the sheet is open; rides through finalize so the order's
    // carrier (and total) is the one the buyer picked.
    let selectedCarrierId = null;
    let shippingCallbacks = {};

    if (isProduct) {
        snapshot = snapshotProductDom();
        if (!snapshot) {
            displayWalletError(interaction, _t(
                "Could not read the product details. Please reload the page and try again.",
            ));
            return;
        }
        currencyCode = container.dataset.currencyCode || '';
        // No cart yet on a product page: carriers were pre-rated off a throwaway
        // order (`_apPrefetchProductMethods`) and the totals are summed
        // client-side, the same way the Woo plugin does it. The real cart + its
        // authoritative total are created on authorize.
        const prefetch = interaction._apProductShipping;
        const methods = prefetch?.delivery_methods || [];
        const subtotal = prefetch ? prefetch.subtotal : snapshot.unitPrice * snapshot.qty;
        if (methods.length) {
            initialMethods = methods.map(_apShippingMethod);
            selectedCarrierId = methods[0].id;
            amount = subtotal + (methods[0].amount || 0);
            requiredShippingContactFields = ['postalAddress', 'name', 'email', 'phone'];
            shippingCallbacks = {
                onShippingMethodSelected: async (method) => {
                    selectedCarrierId = parseInt(method.identifier) || selectedCarrierId;
                    return {
                        newTotal: _apTotalLineItem(storeName, subtotal + parseFloat(method.amount || 0)),
                    };
                },
            };
        } else {
            amount = subtotal;
        }
    } else {
        amount = parseFloat(interaction.paymentContext.amount || 0);
        currencyCode = (interaction.paymentContext.currencyName || '').toUpperCase();
        if (interaction.paymentContext.shippingInfoRequired) {
            requiredShippingContactFields = ['postalAddress', 'name', 'email', 'phone'];
            shippingCallbacks = {
                // Buyer confirmed an address: fetch the serviceable carriers.
                onShippingContactSelected: async (contact) => {
                    let resp;
                    try {
                        resp = await rpc('/shop/buckaroo/wallet/shipping_address', {
                            partial_delivery_address: _apContactToAddress(contact),
                        });
                    } catch (error) {
                        console.error('Apple Pay shipping address update failed:', error);
                        return {
                            errors: _apError('addressUnserviceable'),
                            newShippingMethods: [],
                            newTotal: _apTotalLineItem(storeName, amount),
                        };
                    }
                    const methods = resp.delivery_methods || [];
                    // Odoo preselects the cheapest server-side; Apple preselects
                    // the first entry, so keep them in sync.
                    selectedCarrierId = methods.length ? methods[0].id : null;
                    return {
                        errors: methods.length ? [] : _apError('addressUnserviceable'),
                        newShippingMethods: methods.map(_apShippingMethod),
                        newTotal: _apTotalLineItem(storeName, resp.amount),
                    };
                },
                // Buyer switched method: persist it and return the new total.
                onShippingMethodSelected: _apSetMethodHandler(storeName, amount, (method) => {
                    selectedCarrierId = parseInt(method.identifier) || selectedCarrierId;
                }),
            };
        }
    }

    let authResult;
    try {
        authResult = await _apAuthorize('#' + container.id, {
            storeName,
            countryCode: container.dataset.countryCode,
            currencyCode,
            cultureCode: document.documentElement.lang,
            merchantGuid: container.dataset.merchantGuid,
            amount,
            requiredBillingContactFields: ['postalAddress', 'name', 'email'],
            requiredShippingContactFields,
            shippingMethods: initialMethods,
            ...shippingCallbacks,
        });
    } catch (error) {
        console.error('Apple Pay authorization failed:', error);
        displayWalletError(interaction, _t(
            "Apple Pay payment could not be processed. Please try again.",
        ));
        return;
    }

    const { token, customerName, billingContact, shippingContact } = authResult;
    if (!token || !customerName) {
        // User cancelled the sheet or Apple returned an incomplete response.
        return;
    }

    // Bind the variant to a cart only after the sheet returns: the finalize
    // step needs a transaction route and partner id.
    if (isProduct && !(await bindProductCart(
        interaction,
        '/shop/buckaroo/applepay/express_init',
        snapshot,
        _t("Apple Pay could not start. Please try again."),
    ))) {
        return;
    }

    const ctx = interaction.paymentContext;
    const addresses = { billing_address: _apContactToAddress(billingContact) };
    if (ctx.shippingInfoRequired && shippingContact) {
        addresses.shipping_address = _apContactToAddress(shippingContact);
    }
    if (selectedCarrierId) {
        // Re-apply the picked carrier so its cost is in the order total Buckaroo charges.
        addresses.shipping_option = { id: String(selectedCarrierId) };
    }
    await finalizeExpressTransaction(interaction, {
        providerId: container.dataset.providerId,
        paymentMethodId,
        addresses,
        txExtra: {
            buckaroo_applepay_token: token,
            buckaroo_applepay_customer_name: customerName,
        },
        txError: _t("Apple Pay payment could not be processed. Please try again."),
    });
}

// Apple Pay drives the cart through a standalone container + interaction
// rather than the `ExpressCheckout` patch Google Pay uses: the patch model
// owns a single `o_express_checkout_container`, so both wallets coexisting in
// one express form needs a distinct container. The same class serves the
// product page via `data-placement="product"`.
export class BuckarooApplePayExpress extends ExpressCheckout {
    static selector = 'div[name="o_buckaroo_applepay_express_container"]';

    setup() {
        this.paymentContext = { ...this.el.dataset };
        this.paymentContext.shippingInfoRequired = truthyData(this.paymentContext.shippingInfoRequired);
        if (this.paymentContext.placement !== 'product') {
            // Cart: the live express context (routes, partner, amount) lives
            // on the enclosing express form, not the container.
            const form = this.el.closest('form[name="o_payment_express_checkout_form"]');
            const d = form?.dataset || {};
            Object.assign(this.paymentContext, {
                amount: d.amount,
                minorAmount: d.minorAmount,
                currencyName: d.currencyName,
                partnerId: d.partnerId,
                transactionRoute: d.transactionRoute,
                expressCheckoutRoute: d.expressCheckoutRoute,
                accessToken: d.accessToken,
                landingRoute: d.landingRoute || '/shop/payment/validate',
                shippingInfoRequired: truthyData(d.shippingInfoRequired),
            });
        }
    }

    async willStart() {
        await _apExpressMountButton(this);
    }

    start() {
        if (this.paymentContext.placement === 'product') {
            // Re-rate carriers when the variant or quantity changes so the sheet
            // (and its free-over thresholds) stays accurate at click time.
            const form = this.el.closest('form') || document;
            this._apRefresh = () => _apPrefetchProductMethods(this);
            form.addEventListener('change', this._apRefresh);
            return;
        }
        // Keep the sheet amount fresh when the cart quantity changes.
        this.env.bus.addEventListener('cart_amount_changed', (ev) =>
            this._updateAmount(...ev.detail));
    }

    _updateAmount(newAmount, newMinorAmount) {
        this.paymentContext.amount = parseFloat(newAmount);
        this.paymentContext.minorAmount = parseInt(newMinorAmount);
        if (this._apMounted) {
            this.el.classList.toggle('d-none', this.paymentContext.amount === 0);
        }
    }
}

registry
    .category('public.interactions')
    .add('payment_buckaroo_official.applepay_express', BuckarooApplePayExpress);
