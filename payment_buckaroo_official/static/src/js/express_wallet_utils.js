/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { loadJS } from "@web/core/assets";
import { rpc } from '@web/core/network/rpc';
import { redirect } from '@web/core/utils/urls';
import { ConfirmationDialog } from '@web/core/confirmation_dialog/confirmation_dialog';

// Generic express-button helpers shared by the Buckaroo wallets (Apple Pay,
// Google Pay, PayPal). No method-specific branching lives here.

// A wallet sheet's address and carrier callbacks write to the order (the
// carrier sets the delivery line). If the sheet then closes without paying,
// the page (summary, every wallet button's amount) still shows the old total,
// and another wallet would authorize it. Call `markWalletSheetChangedOrder`
// from those callbacks; a cancel or error then reloads the page so everything
// matches the order again (BTI-2182).
export function markWalletSheetChangedOrder(state) {
    state._walletSheetChangedOrder = true;
}

// Returns whether it reloads (the caller then stops).
export function reloadIfWalletSheetChangedOrder(state) {
    if (!state._walletSheetChangedOrder) {
        return false;
    }
    state._walletSheetChangedOrder = false;
    window.location.reload();
    return true;
}

// Why a checkout wallet can't take a payment, or null when it can: nothing is
// left to pay (a giftcard covered the order), or its amount couldn't be
// computed (the template renders "None"). No sheet or PayPal order then.
export function walletCheckoutBlocker(amount) {
    const total = parseFloat(amount);
    if (total > 0) {
        return null;
    }
    return total === 0
        ? _t("Nothing is left to pay for this order.")
        : _t("This payment method is not available right now. Please choose another payment method.");
}

// Say `message` (a `walletCheckoutBlocker`) in place of the wallet's button.
export function showWalletBlocked(container, message) {
    container.classList.add('d-none');
    if (container.nextElementSibling?.classList.contains('o_buckaroo_wallet_blocked')) {
        return;
    }
    const notice = document.createElement('div');
    notice.className = 'o_buckaroo_wallet_blocked alert alert-info mb-0';
    notice.textContent = message;
    container.after(notice);
}

export function displayWalletError(interaction, message) {
    interaction.services.dialog.add(ConfirmationDialog, {
        title: _t("Error"),
        body: message,
    }, {
        onClose: () => reloadIfWalletSheetChangedOrder(interaction),
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

// A wallet button shows (Apple Pay, Google Pay) or authorizes (PayPal) the
// amount it holds when clicked, and the click must open the sheet at once, so
// it can't wait for the server then. That amount must equal what the
// transaction charges: the method's Buckaroo fee and, after a partial giftcard
// payment, only the open remainder (BTI-2182). The server computes it ahead of
// the click. While a refresh is in flight, or after one failed, the wallet's
// container `el` is inert (no mouse, keyboard or focus, inside a wallet iframe
// too). A failed refresh is retried a few times before `el` stays inert, so a
// network blip doesn't block the button. Refreshes can overlap (quick quantity
// or variant changes): only the latest settles `el`. `state` (the interaction)
// holds the request counter.
// Resolves with the response, `null` when the refresh failed, or `undefined`
// when a newer refresh superseded it.
function _startWalletAmountRequest(state, el) {
    state._walletAmountRequest = (state._walletAmountRequest || 0) + 1;
    el.inert = true;
    el.classList.add('opacity-50');
    return state._walletAmountRequest;
}

function _settleWalletAmountRequest(state, el, request, ok) {
    if (request !== state._walletAmountRequest) {
        return false;
    }
    el.inert = !ok;
    el.classList.toggle('opacity-50', !ok);
    return true;
}

const WALLET_AMOUNT_RETRY_DELAYS_MS = [1000, 3000];

async function _walletAmountRequest(state, el, route, params) {
    const request = _startWalletAmountRequest(state, el);
    let resp = null;
    for (const delay of [0, ...WALLET_AMOUNT_RETRY_DELAYS_MS]) {
        if (delay) {
            await new Promise((resolve) => setTimeout(resolve, delay));
        }
        if (request !== state._walletAmountRequest) {
            return undefined;
        }
        try {
            resp = await rpc(route, params);
            break;
        } catch (error) {
            console.error(`Wallet amount refresh failed (${route}):`, error);
        }
    }
    // The routes answer an empty placeholder (no `currency_code`) when there is
    // no cart or product: no amount to charge, so the button stays inert.
    const ok = !!resp?.currency_code;
    if (!_settleWalletAmountRequest(state, el, request, ok)) {
        return undefined;
    }
    return ok ? resp : null;
}

// Cart total the wallet charges: `{ amount, minor_amount, currency_code }`.
export function fetchWalletCartAmount(state, el, paymentMethodId) {
    return _walletAmountRequest(state, el, '/shop/buckaroo/wallet/amount', {
        payment_method_id: paymentMethodId,
    });
}

// Product page (no cart yet): `{ subtotal, delivery_methods }` rated off a
// throwaway order, the subtotal including the method's fee. An unreadable
// product resolves `null` but leaves `el` usable, so the wallet's click can
// tell the buyer.
export async function fetchWalletProductPrefetch(state, el, paymentMethodId) {
    const snap = snapshotProductDom();
    if (!snap) {
        // Still a new request, so an older prefetch can't land after it.
        _settleWalletAmountRequest(state, el, _startWalletAmountRequest(state, el), true);
        return null;
    }
    return _walletAmountRequest(state, el, '/shop/buckaroo/wallet/product_methods', {
        product_id: snap.productId,
        qty: snap.qty,
        payment_method_id: paymentMethodId,
    });
}

// Product pages have no cart yet: bind the shown variant to a throwaway cart
// (express_init) and copy its authoritative routes/amount/partner onto the
// interaction's paymentContext. Returns false (showing `startError`) on failure.
// Colibri adds an interaction's listeners (`cart_amount_changed` on the bus,
// `change` on the product form) in `start()`, only after `willStart`
// resolved, and a mount refresh can take seconds with retries. Run `refresh`
// again while `eventName` fired on `target` meanwhile, so the button never
// keeps the total from before a quantity or variant change.
export async function refreshDuringMount(target, eventName, refresh) {
    let changed = false;
    const onChange = () => {
        changed = true;
    };
    target.addEventListener(eventName, onChange);
    try {
        do {
            changed = false;
            await refresh();
        } while (changed);
    } finally {
        target.removeEventListener(eventName, onChange);
    }
}

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
    // From here the order takes the buyer's address, and the transaction route
    // may refuse (e.g. the total changed): any error then reloads the page on
    // close, so the next attempt starts from the current order.
    markWalletSheetChangedOrder(interaction);
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
