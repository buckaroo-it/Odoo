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
            # Serialize concurrent pushes for the same transaction. Buckaroo can
            # deliver several pushes for one order at once (e.g. two separate
            # refunds, each spawning a child whose reference core derives from
            # the parent as ``R-{order}``). Odoo cursors run at REPEATABLE READ,
            # so a blocking lock wouldn't help: the second push keeps its stale
            # snapshot, recomputes the same reference and still hits the unique
            # constraint (HTTP 422 "Reference must be unique!"). NOWAIT instead
            # raises LockNotAvailable, which Odoo's request layer retries with a
            # fresh snapshot; the retry sees the committed sibling and computes
            # the next reference (``R-{order}-1``). This relies on push
            # processing below staying free of external side-effects (gateway
            # calls, outbound mail) so a retried request is safe to re-run.
            req.env.cr.execute(
                "SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE NOWAIT",
                [tx_sudo.id],
            )
            remainder_tx = tx_sudo._buckaroo_split_remainder_push(parsed)
            target = remainder_tx or tx_sudo
            target._process("buckaroo_official", parsed)
            # A push confirms the payment server-to-server and never reaches
            # /payment/status, so the framework's status-poll never post-processes
            # it; account.payment would otherwise only appear after the 10-min
            # cron. Post-process inline instead (also covers spawned siblings,
            # which skip the poll route too).
            if target.state in ("done", "authorized", "cancel") and not target.is_post_processed:
                target._post_process()
