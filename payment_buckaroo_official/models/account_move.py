# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..utils import const


class AccountMove(models.Model):
    _inherit = "account.move"

    valid_for_buckaroo_official_refund = fields.Boolean(
        compute="_compute_valid_for_buckaroo_official_refund",
    )

    def _find_valid_buckaroo_official_transactions(self):
        self.ensure_one()

        def _is_buckaroo_source(tx):
            return (
                tx.state == "done"
                and tx.provider_code == const.PROVIDER_CODE
                and tx.operation != "refund"
                and tx.company_id == self.company_id
            )

        transactions = self.reversed_entry_id.transaction_ids.filtered(_is_buckaroo_source)
        if not transactions and "sale_line_ids" in self.invoice_line_ids._fields:
            transactions = self.invoice_line_ids.mapped(
                "sale_line_ids.order_id.transaction_ids"
            ).filtered(_is_buckaroo_source)
        return transactions

    @api.depends(
        "move_type",
        "state",
        "reversed_entry_id.transaction_ids.state",
        "reversed_entry_id.transaction_ids.provider_code",
        "reversed_entry_id.transaction_ids.operation",
        "reversed_entry_id.transaction_ids.payment_id.amount_available_for_refund",
    )
    def _compute_valid_for_buckaroo_official_refund(self):
        for move in self:
            valid = False
            if move.move_type == "out_refund" and move.state == "posted":
                txs = move._find_valid_buckaroo_official_transactions()
                valid = any(
                    tx.payment_id and tx.payment_id.amount_available_for_refund > 0 for tx in txs
                )
            move.valid_for_buckaroo_official_refund = valid

    def action_buckaroo_official_refund(self):
        self.ensure_one()
        transactions = self._find_valid_buckaroo_official_transactions()
        if not transactions:
            raise UserError(_("No Buckaroo transaction is linked to this credit note."))
        if len(transactions) > 1:
            action = self._buckaroo_handle_multi_tx_refund(transactions)
            if action:
                return action
            raise UserError(
                _(
                    "Multiple Buckaroo transactions are linked to this credit "
                    "note. Refund each payment record individually."
                )
            )
        payment = transactions.payment_id
        if not payment:
            raise UserError(_("No payment record is linked to the Buckaroo transaction."))
        if payment.amount_available_for_refund <= 0:
            raise UserError(_("This Buckaroo payment has already been fully refunded."))
        action = payment.action_refund_wizard()
        action["context"] = {"active_id": payment.id}
        return action

    def _buckaroo_handle_multi_tx_refund(self, transactions):
        self.ensure_one()
        return None
