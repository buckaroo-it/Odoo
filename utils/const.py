# Part of Odoo. See LICENSE file for full copyright and licensing details.


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
    "buckaroo_paypermail",
]

class BuckarooStatusCode:
    """Buckaroo gateway transaction status codes, owned by this addon.

    These are the numeric codes the gateway sends on pushes and API
    responses (``brq_statuscode`` / ``Status.Code.Code``); see
    https://docs.buckaroo.io/docs/statuscodes. Held here as plain integers,
    independent of the Python SDK's ``BuckarooStatusCode``: the SDK renamed
    and renumbered its members in 0.2.0 (e.g. ``CANCELLED_BY_MERCHANT`` moved
    891 -> 691), and this addon compares the raw integers below against the
    mapping, so coupling to the SDK enum would break module import or silently
    shift the state mapping on an SDK bump.
    """

    SUCCESS = 190

    PENDING_INPUT = 790
    PENDING_PROCESSING = 791
    AWAITING_CONSUMER = 792
    ON_HOLD = 793

    FAILED = 490
    VALIDATION_FAILURE = 491
    TECHNICAL_FAILURE = 492
    REJECTED = 690

    CANCELLED_BY_USER = 890
    CANCELLED_BY_MERCHANT = 891


# Buckaroo status code groupings -> Odoo tx states. Compared against the raw
# integer status code from the push/response, so the values are what matter.
BUCKAROO_STATUS_CODES_MAPPING = {
    "done": frozenset({BuckarooStatusCode.SUCCESS}),
    "pending": frozenset(
        {
            BuckarooStatusCode.PENDING_INPUT,
            BuckarooStatusCode.PENDING_PROCESSING,
            BuckarooStatusCode.AWAITING_CONSUMER,
            BuckarooStatusCode.ON_HOLD,
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
        }
    ),
}
