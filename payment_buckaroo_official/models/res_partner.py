# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

from odoo import models
from odoo.exceptions import ValidationError
from odoo.tools.partner_identifiers import ADDITIONAL_IDENTIFIERS_METADATA

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = "res.partner"

    def _buckaroo_official_company_registry_key(self):
        """Identifier key of the partner country's main enterprise number
        (Chamber of Commerce), e.g. ``NL_KVK`` or ``BE_EN``; ``None`` if the
        country has none."""
        self.ensure_one()
        country_code = self.commercial_partner_id.country_code
        keys = [
            (metadata.get("sequence", 100), key)
            for key, metadata in ADDITIONAL_IDENTIFIERS_METADATA.items()
            if metadata.get("category") == "EN"
            and country_code in (metadata.get("countries") or ())
        ]
        return min(keys)[1] if keys else None

    def _buckaroo_official_get_company_registry(self):
        """Chamber of Commerce number of the commercial partner, or ``''``."""
        if not self:
            return ""
        self.ensure_one()
        key = self._buckaroo_official_company_registry_key()
        if not key:
            return ""
        return self.commercial_partner_id._get_additional_identifier(key) or ""

    def _buckaroo_official_set_company_registry(self, value):
        """Store *value* as the commercial partner's Chamber of Commerce number
        to prefill the next checkout. A value Odoo rejects for that country is
        not stored; the payment still uses it."""
        self.ensure_one()
        key = self._buckaroo_official_company_registry_key()
        if not key:
            return
        try:
            self.commercial_partner_id._set_additional_identifier(key, value)
        except ValidationError:
            _logger.info("Buckaroo: not storing invalid %s for partner %s", key, self.id)
