# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
import pprint

from odoo import http
from odoo.http import request

from ..utils.push_handlers import parse_push, verify_signature

_logger = logging.getLogger(__name__)


class BuckarooOfficialController(http.Controller):
    _return_url = "/payment/buckaroo_official/return"
    _webhook_url = "/payment/buckaroo_official/webhook"

    @http.route(
        _return_url,
        type="http",
        auth="public",
        methods=["GET", "POST"],
        csrf=False,
        save_session=False,
    )
    def buckaroo_official_return(self, **_kwargs):
        """Handle the return from Buckaroo after the shopper completes or cancels."""
        self._handle_push(request)
        return request.redirect("/payment/status")

    @http.route(
        _webhook_url,
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def buckaroo_official_webhook(self, **_kwargs):
        """Handle Buckaroo Push (server-to-server) notifications."""
        self._handle_push(request)
        return ""

    @staticmethod
    def _handle_push(req):
        """Parse, verify, and process a Buckaroo push or return request."""
        parsed = parse_push(req)
        _logger.info("Buckaroo Official push data:\n%s", pprint.pformat(parsed.raw))

        tx_sudo = (
            req.env["payment.transaction"]
            .sudo()
            ._search_by_reference(
                "buckaroo_official",
                parsed,
            )
        )
        if tx_sudo:
            verify_signature(parsed, tx_sudo.provider_id)
            remainder_tx = tx_sudo._buckaroo_split_remainder_push(parsed)
            target = remainder_tx or tx_sudo
            target._process("buckaroo_official", parsed)
            if remainder_tx and remainder_tx.state == "done":
                # Spawned siblings skip the framework's status-poll route, so
                # post-processing won't fire on its own.
                remainder_tx._post_process()
