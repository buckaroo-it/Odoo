# Part of Odoo. See LICENSE file for full copyright and licensing details.

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

# Buckaroo status code groupings.
# See https://docs.buckaroo.io/docs/statuscodes
BUCKAROO_STATUS_CODES_MAPPING = {
    'done': [190],       # Success
    'pending': [790, 791, 792, 793],  # Pending processing / awaiting input
    'cancel': [890, 891],  # Cancelled by user / merchant
    'error': [490, 491, 492, 690, 691, 692],  # Failed / validation error / rejected
}
