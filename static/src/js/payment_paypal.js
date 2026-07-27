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

// Buckaroo's SDK owns the PayPal order creation + capture; its `PayPal.initiate`
// renders the PayPal Buttons and pulls in PayPal's own JS SDK on demand. The two
// hosts are distinct environment builds: the live one creates the order on prod
// and loads PayPal with the live client id; the test one targets testcheckout and
// loads PayPal's sandbox client id. A test-mode provider creates a sandbox order,
// so it must load the test SDK or the buyer approves under the wrong PayPal app
// and the capture fails with "Order ... can't be captured in current state".
const BUCKAROO_SDK_URL = 'https://checkout.buckaroo.nl/api/buckaroosdk/script';
const BUCKAROO_SDK_URL_TEST = 'https://testcheckout.buckaroo.nl/api/buckaroosdk/script';

let _ppSdkPromise = null;

// Memoize the SDK load so every PayPal surface (cart, product, checkout) shares
// one fetch; `loadJS` also dedupes by URL across the other Buckaroo wallets.
function _ppEnsureSdk(testMode) {
    if (!_ppSdkPromise) {
        const url = testMode ? BUCKAROO_SDK_URL_TEST : BUCKAROO_SDK_URL;
        _ppSdkPromise = loadJS(url).catch((error) => {
            _ppSdkPromise = null;
            throw error;
        });
    }
    return _ppSdkPromise;
}

// PayPal's `onShippingChange` only exposes a partial address (city, state,
// postal, country - no name/street/email). Map it to the address shape the
// express-checkout route consumes; `name` and `country` keys are required (the
// route .pop's country and crashes without name). The push handler overwrites
// this stub with the buyer's real PayPal address.
function _ppPartialToAddress(shippingAddress) {
    const a = shippingAddress || {};
    return {
        name: 'PayPal Customer',
        email: '',
        phone: '',
        street: '',
        street2: '',
        zip: a.postal_code || '',
        city: a.city || '',
        country: (a.country_code || '').toUpperCase(),
        state: a.state || '',
    };
}

function _ppCurrencyCode(interaction, container) {
    return (interaction.paymentContext.currencyName || container.dataset.currencyCode || '').toUpperCase();
}

// Product subtotal (+ cheapest carrier) summed client-side: a product page has
// no cart yet, so carriers are pre-rated off a throwaway order and the totals
// are computed here, the same way the Apple Pay product path does it.
function _ppProductAmount(interaction) {
    const snap = snapshotProductDom();
    const prefetch = interaction._ppProductShipping;
    const subtotal = prefetch ? prefetch.subtotal : (snap ? snap.unitPrice * snap.qty : 0);
    const methods = prefetch?.delivery_methods || [];
    return methods.length ? subtotal + (methods[0].amount || 0) : subtotal;
}

function _ppInitialAmount(interaction, container) {
    if (container.dataset.placement === 'product') {
        return _ppProductAmount(interaction);
    }
    return parseFloat(interaction.paymentContext.amount || 0);
}

// Replace the PayPal sheet's purchase-unit amount with the server-authoritative
// total. Item_total carries the full value (no separate line items); PayPal
// requires the breakdown to sum to the amount.
function _ppPatchAmount(actions, currency, total) {
    const value = parseFloat(total || 0).toFixed(2);
    return actions.order.patch([{
        op: 'replace',
        path: "/purchase_units/@reference_id=='default'/amount",
        value: {
            currency_code: currency,
            value,
            breakdown: { item_total: { currency_code: currency, value } },
        },
    }]);
}

async function _ppPrefetchProductMethods(interaction) {
    const snap = snapshotProductDom();
    if (!snap) {
        interaction._ppProductShipping = null;
        return;
    }
    try {
        interaction._ppProductShipping = await rpc('/shop/buckaroo/wallet/product_methods', {
            product_id: snap.productId,
            qty: snap.qty,
        });
    } catch (error) {
        console.error('PayPal product shipping prefetch failed:', error);
        interaction._ppProductShipping = null;
    }
}

async function _ppRefreshProduct(interaction) {
    await _ppPrefetchProductMethods(interaction);
    if (interaction._ppOptions) {
        // `createOrder` reads `options.amount` lazily at click time, so keeping
        // it current is enough - no need to re-render the button.
        interaction._ppOptions.amount = _ppProductAmount(interaction);
    }
}

// Fires while the PayPal sheet is open. Cart/checkout have a real cart, so the
// carriers and total are server-authoritative; the product page has no cart yet,
// so totals are summed client-side from the prefetch until the cart is bound.
async function _ppOnShippingChange(interaction, options, container, data, actions) {
    interaction._ppLastAddress = data.shipping_address || {};
    const currency = options.currency;

    if (container.dataset.placement === 'product') {
        const methods = interaction._ppProductShipping?.delivery_methods || [];
        const total = _ppProductAmount(interaction);
        interaction._ppSelectedCarrierId = methods.length ? methods[0].id : null;
        options.amount = total;
        return _ppPatchAmount(actions, currency, total);
    }

    let resp;
    try {
        const carrierId = data.selected_shipping_option?.id;
        if (carrierId) {
            // Buyer switched carrier: persist it and read the new total.
            resp = await rpc('/shop/buckaroo/wallet/set_method', { dm_id: carrierId });
            interaction._ppSelectedCarrierId = parseInt(carrierId) || interaction._ppSelectedCarrierId;
        } else {
            // Buyer picked an address: fetch the serviceable carriers (Odoo
            // preselects the cheapest server-side).
            resp = await rpc('/shop/buckaroo/wallet/shipping_address', {
                partial_delivery_address: _ppPartialToAddress(data.shipping_address),
            });
            const methods = resp.delivery_methods || [];
            if (!methods.length) {
                return actions.reject();
            }
            interaction._ppSelectedCarrierId = methods[0].id;
        }
    } catch (error) {
        console.error('PayPal shipping update failed:', error);
        return actions.reject();
    }
    options.amount = parseFloat(resp.amount);
    return _ppPatchAmount(actions, currency, resp.amount);
}

// Product pages have no cart: bind the shown variant to a throwaway cart the
// moment the button is clicked (before PayPal's `createOrder`), so the finalize
// step has a transaction route and partner id. Cart/checkout already have one.
function _ppOnClick(interaction, container) {
    if (container.dataset.placement !== 'product') {
        return;
    }
    const snapshot = snapshotProductDom();
    if (!snapshot) {
        interaction._ppBindPromise = Promise.resolve(false);
        displayWalletError(interaction, _t(
            "Could not read the product details. Please reload the page and try again.",
        ));
        return;
    }
    interaction._ppBindPromise = bindProductCart(
        interaction,
        '/shop/buckaroo/paypal/express_init',
        snapshot,
        _t("PayPal could not start. Please try again."),
    );
}

// PayPal approved the order: stash the PayPal order id (the controller writes it
// to the session for `_buckaroo_create_payment`) and start the transaction.
async function _ppCreatePayment(interaction, container, paymentMethodId, data) {
    if (container.dataset.placement === 'checkout') {
        // No cart to address-update here (the order's address is already
        // known), so skip the express finalize entirely and hand the order id
        // to the core "Pay now" flow via the `PaymentForm` patch below - it
        // injects it into the transaction route and submits normally.
        interaction.env.bus.trigger('buckaroo_paypal_checkout_approved', { orderID: data.orderID });
        return;
    }
    if (container.dataset.placement === 'product') {
        const bound = await interaction._ppBindPromise;
        if (!bound) {
            displayWalletError(interaction, _t("PayPal could not start. Please try again."));
            return;
        }
    }
    const ctx = interaction.paymentContext;
    const address = _ppPartialToAddress(interaction._ppLastAddress);
    const addresses = { billing_address: address };
    if (ctx.shippingInfoRequired) {
        addresses.shipping_address = address;
    }
    if (interaction._ppSelectedCarrierId) {
        // Re-apply the picked carrier so its cost is in the order total Buckaroo charges.
        addresses.shipping_option = { id: String(interaction._ppSelectedCarrierId) };
    }
    await finalizeExpressTransaction(interaction, {
        providerId: container.dataset.providerId,
        paymentMethodId,
        addresses,
        txExtra: { buckaroo_paypal_order_id: data.orderID },
        txError: _t("PayPal payment could not be processed. Please try again."),
    });
}

function _ppInitiate(interaction, container, merchantId, websiteKey, paymentMethodId) {
    const options = {
        containerSelector: '#' + container.id,
        buckarooWebsiteKey: websiteKey,
        paypalMerchantId: merchantId,
        currency: _ppCurrencyCode(interaction, container),
        amount: _ppInitialAmount(interaction, container),
        createPaymentHandler: (data) => _ppCreatePayment(interaction, container, paymentMethodId, data),
        onClickCallback: () => _ppOnClick(interaction, container),
        onCancelCallback: () => { interaction._ppBindPromise = null; },
        // The SDK calls this unconditionally after `createPaymentHandler`
        // resolves; finalize has already redirected, so it is a no-op.
        onSuccessCallback: () => {},
        onErrorCallback: (reason) => {
            console.error('PayPal error:', reason);
            displayWalletError(interaction, _t(
                "PayPal payment could not be processed. Please try again.",
            ));
        },
    };
    if (container.dataset.placement !== 'checkout') {
        // At checkout the buyer already chose address + carrier through the
        // normal Odoo flow; a PayPal-sheet shipping change must not overwrite
        // them, so cart/product are the only placements that get this handler.
        options.onShippingChangeHandler = (data, actions) =>
            _ppOnShippingChange(interaction, options, container, data, actions);
    }
    interaction._ppOptions = options;
    BuckarooSdk.PayPal.initiate(options);
}

// PayPal's Buttons cannot render into a hidden (zero-width) container, so on the
// checkout page the button is mounted only once its inline form is shown. Cart
// and product surfaces are visible at load and mount immediately.
function _ppWhenVisible(el, callback) {
    if (el.offsetParent !== null || typeof IntersectionObserver === 'undefined') {
        callback();
        return;
    }
    const observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
            observer.disconnect();
            callback();
        }
    });
    observer.observe(el);
}

async function _ppMountButton(interaction) {
    const container = interaction.el;
    const merchantId = container.dataset.merchantId || '';
    const websiteKey = container.dataset.websiteKey || '';
    const paymentMethodId = parseInt(container.dataset.paymentMethodId);
    if (!merchantId || !websiteKey || !paymentMethodId || !container.id) {
        container.classList.add('d-none');
        return;
    }
    // Keep only the first cart button when a theme renders the express form twice.
    if (container.dataset.placement !== 'product' && isDuplicatePlacement(
        container,
        'form[name="o_payment_express_checkout_form"] div[name="o_buckaroo_paypal_express_container"][data-merchant-id]',
    )) {
        container.classList.add('d-none');
        return;
    }
    _ppWhenVisible(container, async () => {
        try {
            await _ppEnsureSdk(truthyData(container.dataset.testMode));
        } catch (error) {
            console.error('PayPal SDK load failed:', error);
            container.classList.add('d-none');
            return;
        }
        if (typeof BuckarooSdk === 'undefined' || !BuckarooSdk.PayPal) {
            container.classList.add('d-none');
            return;
        }
        if (container.dataset.placement === 'product') {
            await _ppPrefetchProductMethods(interaction);
        }
        _ppInitiate(interaction, container, merchantId, websiteKey, paymentMethodId);
    });
}

// Checkout-only: the inline form renders PayPal's own button alongside the
// core "Pay now" button, and only the former knows how to open the PayPal
// sheet. Hide "Pay now" while PayPal is selected so it can't be submitted
// without an order id (the direct cause of "PayPal order ID is missing"), and
// carry the order id `_ppCreatePayment` captured through to the transaction
// route once the PayPal button itself drives the submit.
patch(PaymentForm.prototype, {

    setup() {
        super.setup();
        this._buckarooPaypalOrderId = null;
        this.env.bus.addEventListener('buckaroo_paypal_checkout_approved', (ev) =>
            this._buckarooPaypalSubmit(ev.detail.orderID));
    },

    async _buckarooPaypalSubmit(orderID) {
        this._buckarooPaypalOrderId = orderID;
        try {
            await this.submitForm(new Event('BuckarooPaypalCheckoutApproved'));
        } finally {
            // Single-use; clear so a retry can't resubmit a stale order id.
            this._buckarooPaypalOrderId = null;
        }
    },

    async _prepareInlineForm(_providerId, providerCode, _paymentOptionId, paymentMethodCode, _flow) {
        if (providerCode !== 'buckaroo_official' || paymentMethodCode !== 'buckaroo_paypal') {
            await super._prepareInlineForm(...arguments);
            return;
        }
        this._hideInputs();
    },

    _prepareTransactionRouteParams() {
        const params = super._prepareTransactionRouteParams(...arguments);
        if (!this._buckarooPaypalOrderId) {
            return params;
        }
        const radio = this.el.querySelector('input[name="o_payment_radio"]:checked');
        if (!radio
            || radio.dataset.providerCode !== 'buckaroo_official'
            || radio.dataset.paymentMethodCode !== 'buckaroo_paypal') {
            return params;
        }
        params.buckaroo_paypal_order_id = this._buckarooPaypalOrderId;
        return params;
    },
});

// One interaction drives all three placements via `data-placement`. PayPal's
// own button owns the click + popup on cart/product, so its approval finalizes
// the express transaction directly. At checkout a competing "Pay now" button
// exists, so that surface instead hands off to the `PaymentForm` patch above.
export class BuckarooPaypalExpress extends ExpressCheckout {
    static selector = 'div[name="o_buckaroo_paypal_express_container"]';

    setup() {
        this.paymentContext = { ...this.el.dataset };
        this.paymentContext.shippingInfoRequired = truthyData(this.paymentContext.shippingInfoRequired);
        if (this.paymentContext.placement === 'cart') {
            // The live express context (routes, partner, amount) lives on the
            // enclosing express form.
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
        // Checkout: amount and currency come off this container's own dataset
        // (added by the inline-form template) via the spread above. The core
        // payment form carries the transaction route/token/partner for
        // whichever method is submitted - `_ppCreatePayment` hands off to it
        // instead of reading those fields off this interaction.
    }

    async willStart() {
        await _ppMountButton(this);
    }

    start() {
        if (this.paymentContext.placement === 'product') {
            // Re-rate carriers when the variant or quantity changes so the sheet
            // amount stays accurate at click time.
            const form = this.el.closest('form') || document;
            this._ppRefresh = () => _ppRefreshProduct(this);
            form.addEventListener('change', this._ppRefresh);
            return;
        }
        // Keep the sheet amount fresh when the cart quantity changes.
        this.env.bus.addEventListener('cart_amount_changed', (ev) =>
            this._updateAmount(...ev.detail));
    }

    _updateAmount(newAmount, newMinorAmount) {
        this.paymentContext.amount = parseFloat(newAmount);
        this.paymentContext.minorAmount = parseInt(newMinorAmount);
        if (this._ppOptions) {
            this._ppOptions.amount = parseFloat(newAmount);
        }
    }
}

registry
    .category('public.interactions')
    .add('payment_buckaroo_official.paypal_express', BuckarooPaypalExpress);
