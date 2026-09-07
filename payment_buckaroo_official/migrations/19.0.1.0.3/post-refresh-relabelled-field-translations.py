# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging

_logger = logging.getLogger(__name__)

# Fields whose label changed, so every stored translation of it is stale.
RELABELLED_LABELS = (
    ("payment.method", "buckaroo_official_min_amount"),
    ("payment.method", "buckaroo_official_max_amount"),
    # The label itself is unchanged, but its Dutch, German and French
    # translations were a copy of the max-amount label. They are corrected in
    # the .po files, so the stored ones have to go too.
    ("sale.order", "amount_buckaroo_surcharge"),
)

# Fields whose tooltip changed. Narrower than the list above: dropping a
# translation the .po files cannot refill would lose it for good.
RELABELLED_HELPS = (
    ("payment.method", "buckaroo_official_min_amount"),
    ("payment.method", "buckaroo_official_max_amount"),
)

# Keep only ``en_US``, which the field definition has already refreshed.
# Subtracting the other keys rather than rebuilding the object matters: a
# ``jsonb_build_object('en_US', col -> 'en_US')`` on a column without that key
# would store ``{"en_US": null}``, which reads back as an empty label.
_STRIP_TRANSLATIONS = """
    UPDATE ir_model_fields
       SET {column} = {column} - ARRAY(
               SELECT key
                 FROM jsonb_object_keys({column}) AS key
                WHERE key <> 'en_US'
           )
     WHERE (model, name) IN %s
       AND {column} IS NOT NULL
"""


def migrate(cr, version):
    """Drop the stale translations of the fields this version relabelled.

    An upgrade rewrites the ``en_US`` value from the field definition, but
    ``_load_module_terms`` skips a language whose value is already set. Without
    this, a Dutch, German or French merchant would keep reading the old label
    forever: "Buckaroo min amount (EUR)" for a setting that is now called
    "Minimum amount", and a tooltip still naming EUR and telling them to leave
    the limit at 0,00 rather than empty.

    Leaving the other languages empty lets the translation import that runs
    right after this stage refill them from the ``.po`` files. Languages the
    module does not ship fall back to English.
    """
    for column, fields in (
        ("field_description", RELABELLED_LABELS),
        ("help", RELABELLED_HELPS),
    ):
        cr.execute(_STRIP_TRANSLATIONS.format(column=column), (fields,))
        _logger.info("Reset %s translations of %s field(s).", column, cr.rowcount)
