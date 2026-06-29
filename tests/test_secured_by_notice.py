# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Tests for the Buckaroo "Secured by" checkout notice override.

The customer-facing notice for Buckaroo methods must read "Secured by
Buckaroo" instead of the auto-generated "Secured by Buckaroo Official",
on both the method-selection form and the saved-token form (desktop +
mobile). Non-Buckaroo providers keep their original label.
"""

from odoo.fields import Command
from odoo.tests import tagged
from odoo.tools import is_html_empty

from .common import BuckarooOfficialCommon


@tagged("post_install", "-at_install")
class TestSecuredByNotice(BuckarooOfficialCommon):
    def _render_method_form(self, pm, providers):
        view = self.env.ref("payment.method_form")
        return view._render_template(
            view.id,
            {
                "pm_sudo": pm.sudo(),
                "providers_sudo": providers.sudo(),
                "is_selected": False,
                "mode": "payment",
                "show_tokenize_input_mapping": {p.id: False for p in providers},
                "is_html_empty": is_html_empty,
            },
        )

    def _render_token_form(self, token, provider):
        view = self.env.ref("payment.token_form")
        return view._render_template(
            view.id,
            {
                "token_sudo": token.sudo(),
                "provider_sudo": provider.sudo(),
                "allow_token_selection": True,
            },
        )

    def test_method_form_notice_reads_secured_by_buckaroo(self):
        html = self._render_method_form(self.ideal, self.buckaroo)

        self.assertIn("Secured by", html)
        self.assertIn("Buckaroo", html)
        self.assertNotIn("Buckaroo Official", html)

    def test_token_form_notice_reads_secured_by_buckaroo(self):
        token = self._create_token(provider_id=self.buckaroo.id)

        html = self._render_token_form(token, self.buckaroo)

        self.assertIn("Secured by", html)
        self.assertIn("Buckaroo", html)
        self.assertNotIn("Buckaroo Official", html)

    def test_non_buckaroo_method_form_notice_unchanged(self):
        # The dummy provider (code 'none') keeps its auto-generated label.
        label = dict(
            self.dummy_provider._fields["code"]._description_selection(
                self.dummy_provider.env
            )
        )[self.dummy_provider.code]
        self.dummy_provider.payment_method_ids = [Command.link(self.payment_method.id)]

        html = self._render_method_form(self.payment_method, self.dummy_provider)

        self.assertIn(label, html)
