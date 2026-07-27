# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo.tests import BaseCase, tagged

from odoo.addons.payment_buckaroo_official.utils import const


@tagged("post_install", "-at_install")
class TestStatusCodeMapping(BaseCase):
    """Pin the Buckaroo status-code mapping to the documented gateway
    integers. Regression guard for the SDK 0.2.0 break, where the Python
    SDK renamed/renumbered ``BuckarooStatusCode`` members and importing them
    at module top level crashed the addon on load. The mapping is compared
    against the raw integer ``brq_statuscode`` from pushes, so the integer
    values below are the contract, not the names.
    """

    # Documented gateway codes per https://docs.buckaroo.io/docs/statuscodes.
    EXPECTED = {
        "done": {190},
        "pending": {790, 791, 792, 793, 794},
        "cancel": {890, 891},
        "error": {490, 491, 492, 690},
    }

    def test_mapping_matches_documented_gateway_codes(self):
        actual = {group: set(codes) for group, codes in const.BUCKAROO_STATUS_CODES_MAPPING.items()}
        self.assertEqual(actual, self.EXPECTED)

    def test_success_constant(self):
        self.assertEqual(const.BuckarooStatusCode.SUCCESS, 190)

    def test_groups_are_disjoint(self):
        seen = set()
        for codes in const.BUCKAROO_STATUS_CODES_MAPPING.values():
            overlap = seen & set(codes)
            self.assertFalse(overlap, "status code(s) in more than one group: %s" % overlap)
            seen |= set(codes)

    def test_codes_are_plain_integers(self):
        # Decoupled from the SDK enum: values must be plain ints so raw
        # gateway codes match by equality regardless of the installed SDK.
        for codes in const.BUCKAROO_STATUS_CODES_MAPPING.values():
            for code in codes:
                self.assertIs(type(code), int)
