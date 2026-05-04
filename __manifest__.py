# Part of Odoo. See LICENSE file for full copyright and licensing details.

{
    'name': 'Payment Provider: Buckaroo Official',
    'version': '1.0',
    'category': 'Accounting/Payment Providers',
    'sequence': 350,
    'summary': "An official Buckaroo payment provider for Odoo 19.",
    'description': " ",
    'depends': ['payment', 'website_sale'],
    'external_dependencies': {
        'python': ['buckaroo'],
    },
    'data': [
        'views/payment_provider_views.xml',
        'views/payment_method_views.xml',
        'views/payment_redirect_templates.xml',
        'views/payment_form_templates.xml',
        'views/payment_creditcard_templates.xml',
        'views/payment_billink_templates.xml',
        'views/payment_klarna_templates.xml',
        'views/payment_riverty_templates.xml',
        'data/payment_method_data.xml',
        'data/payment_provider_data.xml',
        'data/product_buckaroo_surcharge.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'payment_buckaroo_official/static/src/css/creditcard.css',
            'payment_buckaroo_official/static/src/js/payment_form_billink.js',
            'payment_buckaroo_official/static/src/js/payment_form_klarna.js',
            'payment_buckaroo_official/static/src/js/payment_form_riverty.js',
            'payment_buckaroo_official/static/src/js/payment_form_creditcard.js',
            'payment_buckaroo_official/static/src/js/payment_form_surcharge.js',
        ],
    },
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'author': 'Buckaroo',
    'license': 'LGPL-3',
}
