# Part of Odoo. See LICENSE file for full copyright and licensing details.

from buckaroo.models.payment_response import BuckarooStatusCode


PROVIDER_CODE = "buckaroo_official"

PICK_METHOD_CONTEXT_KEY = "buckaroo_official_pick_method_id"

DEFAULT_PAYMENT_METHOD_CODES = [
    "buckaroo_ideal",
    "buckaroo_bancontact",
    "buckaroo_wero",
    "buckaroo_eps",
    "buckaroo_belfius",
    "buckaroo_kbc",
    "buckaroo_alipay",
    "buckaroo_wechatpay",
    "buckaroo_payconiq",
    "buckaroo_swish",
    "buckaroo_bizum",
    "buckaroo_mbway",
    "buckaroo_multibanco",
    "buckaroo_knaken",
    "buckaroo_paypal",
    "buckaroo_trustly",
    "buckaroo_przelewy24",
    "buckaroo_blik",
    "buckaroo_twint",
    "buckaroo_googlepay",
    "buckaroo_applepay",
    "buckaroo_billink",
    "buckaroo_in3",
    "buckaroo_giftcard",
    "buckaroo_klarna",
    "buckaroo_riverty",
    "buckaroo_creditcard",
    "buckaroo_bank_transfer",
]

# Buckaroo status code groupings — addon-owned mapping, kept in sync
# with the gateway's documented codes. See
# https://docs.buckaroo.io/docs/statuscodes
BUCKAROO_STATUS_CODES_MAPPING = {
    "done": frozenset({BuckarooStatusCode.SUCCESS}),
    "pending": frozenset(
        {
            BuckarooStatusCode.PENDING_INPUT,
            BuckarooStatusCode.PENDING_PROCESSING,
            BuckarooStatusCode.PENDING_CONSUMER,
            BuckarooStatusCode.AWAITING_TRANSFER,
        }
    ),
    "cancel": frozenset(
        {
            BuckarooStatusCode.CANCELLED_BY_USER,
            BuckarooStatusCode.CANCELLED_BY_MERCHANT,
        }
    ),
    "error": frozenset(
        {
            BuckarooStatusCode.FAILED,
            BuckarooStatusCode.VALIDATION_FAILURE,
            BuckarooStatusCode.TECHNICAL_FAILURE,
            BuckarooStatusCode.REJECTED,
            BuckarooStatusCode.REJECTED_BY_USER,
            BuckarooStatusCode.REJECTED_TECHNICAL,
        }
    ),
}
