/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { loadJS } from "@web/core/assets";
import { rpc } from '@web/core/network/rpc';
import { redirect } from '@web/core/utils/urls';
import { ConfirmationDialog } from '@web/core/confirmation_dialog/confirmation_dialog';

// Generic express-button helpers shared by the Buckaroo wallets (Apple Pay,
// Google Pay, PayPal). No method-specific branching lives here.

export function displayWalletError(interaction, message) {
    interaction.services.dialog.add(ConfirmationDialog, {
        title: _t("Error"),
        body: message,
    });
}

// The live and test SDK builds differ in their checkout host and PayPal client
// id, and both write the one `BuckarooSdk` global: the build that runs last
// wins for every wallet. So every wallet on a page loads the build that
// matches the provider mode, or PayPal can end up creating a live order for a
// sandbox merchant.
const BUCKAROO_SDK_URL = 'https://checkout.buckaroo.nl/api/buckaroosdk/script';
const BUCKAROO_SDK_URL_TEST = 'https://testcheckout.buckaroo.nl/api/buckaroosdk/script';

export function buckarooSdkUrl(testMode) {
    return testMode ? BUCKAROO_SDK_URL_TEST : BUCKAROO_SDK_URL;
}

// The Buckaroo SDK reads the global `jQuery` once, when its script runs, into a
// closure its wallets keep using (Apple Pay: `$(selector)`, `.on()`, `$.post()`
// for merchant validation). Odoo 20 has no frontend jQuery, so the SDK would
// throw and leave `BuckarooSdk` empty. Lend it a vendored jQuery only while SDK
// scripts load, then take it off `window` again; the SDK keeps its reference.
// Bootstrap treats any `window.jQuery` as its own (and triggers jQuery events),
// so `data-bs-no-jquery` keeps it on native events meanwhile. Loads are
// memoized per URL; a failed load can retry.
const SDK_JQUERY_URL = '/payment_buckaroo_official/static/lib/jquery/jquery-3.7.1.min.js';
const _buckarooSdkLoads = {};
let _sdkJQuery = null;
let _sdkJQueryLoad = null;
let _bsNoJQuerySet = false;
let _pendingSdkLoads = 0;

function _loadSdkJQuery() {
    if (!_sdkJQueryLoad) {
        // `noConflict(true)` restores the previous (absent) globals at once.
        _sdkJQueryLoad = loadJS(SDK_JQUERY_URL).then(() => window.jQuery.noConflict(true));
        _sdkJQueryLoad.catch(() => {
            _sdkJQueryLoad = null;
        });
    }
    return _sdkJQueryLoad;
}

async function _withSdkJQuery(load) {
    if (window.jQuery !== undefined && window.jQuery !== _sdkJQuery) {
        return load(); // The page has its own jQuery (Odoo 19).
    }
    if (!_pendingSdkLoads++ && !document.body.hasAttribute('data-bs-no-jquery')) {
        document.body.setAttribute('data-bs-no-jquery', '');
        _bsNoJQuerySet = true;
    }
    try {
        _sdkJQuery = await _loadSdkJQuery();
        window.jQuery = _sdkJQuery;
        return await load();
    } finally {
        if (!--_pendingSdkLoads) {
            if (window.jQuery === _sdkJQuery) {
                delete window.jQuery;
            }
            if (_bsNoJQuerySet) {
                document.body.removeAttribute('data-bs-no-jquery');
                _bsNoJQuerySet = false;
            }
        }
    }
}

export function loadBuckarooSdk(url) {
    if (!_buckarooSdkLoads[url]) {
        _buckarooSdkLoads[url] = _withSdkJQuery(() => loadJS(url)).catch((error) => {
            delete _buckarooSdkLoads[url];
            throw error;
        });
    }
    return _buckarooSdkLoads[url];
}

// Variant changes are not tracked via events; read fresh DOM at click time.
export function snapshotProductDom() {
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

// dataset values are strings; "False" is truthy via `!!`, so coerce explicitly.
export function truthyData(v) {
    return v === 'True' || v === 'true';
}

// Some themes render the express form twice (cart sidebar + shorter_cart_summary).
// True when `container` is a later duplicate the caller should skip.
export function isDuplicatePlacement(container, selector) {
    const all = document.querySelectorAll(selector);
    return all.length > 1 && all[0] !== container;
}

// Product pages have no cart yet: bind the shown variant to a throwaway cart
// (express_init) and copy its authoritative routes/amount/partner onto the
// interaction's paymentContext. Returns false (showing `startError`) on failure.
export async function bindProductCart(interaction, route, snapshot, startError) {
    let payload;
    try {
        payload = await rpc(route, { product_id: snapshot.productId, qty: snapshot.qty });
    } catch (error) {
        console.error('Express product init failed:', error);
        displayWalletError(interaction, startError);
        return false;
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
    return true;
}

// Shared express tail: push the buyer's address (and picked carrier) to the
// express route, then start the transaction. The caller shapes `addresses` and
// `txExtra` (the wallet token + customer name) so this stays method-agnostic;
// `txError` is the wallet-specific "could not be processed" text.
export async function finalizeExpressTransaction(
    interaction, { providerId, paymentMethodId, addresses, txExtra, txError },
) {
    const ctx = interaction.paymentContext;
    let partnerId;
    try {
        partnerId = await interaction.waitFor(rpc(ctx.expressCheckoutRoute, addresses));
    } catch (error) {
        console.error('Express address update RPC failed:', error);
        displayWalletError(interaction, _t("Address update failed. Please try again."));
        return;
    }
    const parsedPartnerId = parseInt(partnerId);
    if (!Number.isFinite(parsedPartnerId)) {
        console.error('Express address update returned invalid partner id:', partnerId);
        displayWalletError(interaction, _t("Address update failed. Please try again."));
        return;
    }
    ctx.partnerId = parsedPartnerId;

    let processingValues;
    try {
        processingValues = await interaction.waitFor(rpc(ctx.transactionRoute, {
            provider_id: parseInt(providerId),
            // Override the framework default of payment_method_unknown so the
            // backend dispatches to the wallet branch.
            payment_method_id: paymentMethodId,
            token_id: null,
            flow: 'direct',
            tokenization_requested: false,
            landing_route: ctx.landingRoute,
            access_token: ctx.accessToken,
            csrf_token: odoo.csrf_token,
            ...txExtra,
        }));
    } catch (error) {
        console.error('Express transaction RPC failed:', error);
        // Show the server's reason (e.g. a ValidationError) when it has one.
        displayWalletError(interaction, error?.data?.message || txError);
        return;
    }

    // Odoo 20 can refuse without raising when a checkout step fails (e.g. an
    // incomplete address, or a login the website requires): no payment was
    // started, so show why, or go to the page that fixes it.
    if (processingValues?.state === 'error') {
        if (!processingValues.state_message && processingValues.redirect) {
            redirect(processingValues.redirect);
            return;
        }
        displayWalletError(interaction, processingValues.state_message || txError);
        return;
    }

    // Buckaroo redirect-based flow: tx returns api_url to its hosted page.
    // Cross-origin, so window.location.assign (Odoo's redirect() blocks foreign origins).
    if (processingValues?.api_url) {
        window.location.assign(processingValues.api_url);
        return;
    }
    redirect('/payment/status');
}
