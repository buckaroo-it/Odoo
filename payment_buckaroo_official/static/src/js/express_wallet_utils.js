/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { rpc } from '@web/core/network/rpc';
import { redirect } from '@web/core/utils/urls';
import { ConfirmationDialog } from '@web/core/confirmation_dialog/confirmation_dialog';

// Generic express-button helpers shared by the Buckaroo wallets (Apple Pay +
// Google Pay). No method-specific branching lives here.

export function displayWalletError(interaction, message) {
    interaction.services.dialog.add(ConfirmationDialog, {
        title: _t("Error"),
        body: message,
    });
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
        displayWalletError(interaction, txError);
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
