# Part of Odoo. See LICENSE file for full copyright and licensing details.

from buckaroo.models.payment_response import BuckarooStatusCode


PROVIDER_CODE = 'buckaroo_official'

PICK_METHOD_CONTEXT_KEY = 'buckaroo_official_pick_method_id'

BUCKAROO_OAUTH_TOKEN_URL = 'https://auth.buckaroo.io/oauth/token'

DEFAULT_PAYMENT_METHOD_CODES = [
    'ideal',
    'bancontact',
    'wero',
    'eps',
    'belfius',
    'kbc',
    'alipay',
    'wechatpay',
    'payconiq',
    'swish',
    'bizum',
    'mbway',
    'multibanco',
    'knaken',
    'paypal',
    'trustly',
    'przelewy24',
    'blik',
    'twint',
    'billink',
    'creditcard',
]

# Buckaroo status code groupings — addon-owned mapping, kept in sync
# with the gateway's documented codes. See
# https://docs.buckaroo.io/docs/statuscodes
BUCKAROO_STATUS_CODES_MAPPING = {
    'done': frozenset({BuckarooStatusCode.SUCCESS}),
    'pending': frozenset({
        BuckarooStatusCode.PENDING_INPUT,
        BuckarooStatusCode.PENDING_PROCESSING,
        BuckarooStatusCode.PENDING_CONSUMER,
        BuckarooStatusCode.AWAITING_TRANSFER,
    }),
    'cancel': frozenset({
        BuckarooStatusCode.CANCELLED_BY_USER,
        BuckarooStatusCode.CANCELLED_BY_MERCHANT,
    }),
    'error': frozenset({
        BuckarooStatusCode.FAILED,
        BuckarooStatusCode.VALIDATION_FAILURE,
        BuckarooStatusCode.TECHNICAL_FAILURE,
        BuckarooStatusCode.REJECTED,
        BuckarooStatusCode.REJECTED_BY_USER,
        BuckarooStatusCode.REJECTED_TECHNICAL,
    }),
}
