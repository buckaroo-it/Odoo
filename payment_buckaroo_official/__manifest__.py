# Part of Odoo. See LICENSE file for full copyright and licensing details.

{
    "name": "Buckaroo Official",
    "version": "19.0.1.0.0",
    "category": "Accounting/Payment Providers",
    "sequence": 350,
    "summary": "An official Buckaroo payment provider for Odoo 19.",
    "description": """
Buckaroo Payment Provider
=========================

Accept payments in Odoo through Buckaroo, with 30+ payment methods across Europe:
iDEAL, Bancontact, credit cards, PayPal, Klarna, in3, Riverty, Billink, Apple Pay,
Google Pay, gift cards and more. Includes express checkout, refunds from Odoo,
authorize & capture, Pay Per Email and optional payment surcharges.
""",
    "depends": ["payment", "website_sale", "account_payment"],
    "external_dependencies": {
        "python": ["buckaroo"],
    },
    "data": [
        "views/payment_provider_views.xml",
        "views/payment_method_views.xml",
        "views/account_move_views.xml",
        "views/payment_redirect_templates.xml",
        "views/payment_form_templates.xml",
        "views/payment_creditcard_templates.xml",
        "views/payment_giftcard_templates.xml",
        "views/payment_giftcard_summary_templates.xml",
        "views/payment_billink_templates.xml",
        "views/payment_in3_templates.xml",
        "views/payment_googlepay_templates.xml",
        "views/payment_applepay_templates.xml",
        "views/payment_paypal_templates.xml",
        "views/payment_klarna_templates.xml",
        "views/payment_paypermail_templates.xml",
        "views/payment_riverty_templates.xml",
        "views/payment_bank_transfer_templates.xml",
        "data/payment_method_data.xml",
        "data/payment_provider_data.xml",
        "data/product_buckaroo_surcharge.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "payment_buckaroo_official/static/src/css/creditcard.css",
            "payment_buckaroo_official/static/src/css/wallet_buttons.css",
            "payment_buckaroo_official/static/src/js/payment_form_billink.js",
            "payment_buckaroo_official/static/src/js/payment_form_in3.js",
            "payment_buckaroo_official/static/src/js/payment_form_klarna.js",
            "payment_buckaroo_official/static/src/js/payment_form_paypermail.js",
            "payment_buckaroo_official/static/src/js/payment_form_riverty.js",
            "payment_buckaroo_official/static/src/js/payment_form_creditcard.js",
            "payment_buckaroo_official/static/src/js/payment_form_giftcard.js",
            "payment_buckaroo_official/static/src/js/express_wallet_utils.js",
            "payment_buckaroo_official/static/src/js/payment_googlepay.js",
            "payment_buckaroo_official/static/src/js/payment_applepay.js",
            "payment_buckaroo_official/static/src/js/payment_paypal.js",
            "payment_buckaroo_official/static/src/js/payment_form_surcharge.js",
        ],
    },
    "images": ["static/description/cover.png"],
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "author": "Buckaroo",
    "website": "https://www.buckaroo.eu/",
    "support": "support@buckaroo.nl",
    "license": "Other OSI approved licence",
}
