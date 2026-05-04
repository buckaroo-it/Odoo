# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Shared BNPL article helpers."""


def get_order_articles(transaction):
    """Return a list of generic article dicts for *transaction*.

    Each dict has snake_case keys: identifier, description, quantity,
    unit_price_incl, unit_price_excl, vat_percentage, vat_amount, type
    ('product' | 'shipping' | 'rounding'), and product (the resolved
    recordset, or ``None`` for synthetic rounding entries). A rounding
    entry is appended when the line-item sum doesn't match
    ``transaction.amount``.
    """
    articles = []
    line_total = 0.0

    for order in transaction.sale_order_ids:
        # Batch-prefetch relational fields once so the per-line reads
        # below don't fan out into one SELECT per cold attribute.
        if hasattr(order.order_line, 'mapped'):
            order.order_line.mapped('product_id.default_code')
            order.order_line.mapped('tax_ids.amount_type')
        for line in order.order_line:
            if line.display_type:
                continue
            product = line.product_id
            identifier = product.default_code or str(product.id)
            description = line.name or product.display_name or identifier

            qty = line.product_uom_qty
            if not qty:
                continue

            price_unit_incl = round(line.price_total / qty, 2)
            price_unit_excl = round(line.price_subtotal / qty, 2)

            # VAT %: computed tax amount → tax record → price difference.
            price_tax = getattr(line, 'price_tax', None)
            has_price_tax = isinstance(price_tax, (int, float))
            vat_pct = 0.0
            if line.price_subtotal and has_price_tax:
                vat_pct = round(price_tax / line.price_subtotal * 100, 2)
            elif line.tax_ids:
                for tax in line.tax_ids:
                    if tax.amount_type == 'percent':
                        vat_pct = round(tax.amount, 2)
                        break
            elif price_unit_excl and price_unit_incl != price_unit_excl:
                vat_pct = round(
                    (price_unit_incl - price_unit_excl) / price_unit_excl * 100,
                    2,
                )

            vat_amount = (
                round(price_tax, 2)
                if has_price_tax
                else round(line.price_total - line.price_subtotal, 2)
            )

            is_delivery = getattr(line, 'is_delivery', False)
            qty_float = float(qty)
            line_total += round(qty_float * price_unit_incl, 2)

            articles.append({
                'identifier': identifier,
                'description': description,
                'quantity': qty_float,
                'unit_price_incl': price_unit_incl,
                'unit_price_excl': price_unit_excl,
                'vat_percentage': vat_pct,
                'vat_amount': vat_amount,
                'type': 'shipping' if is_delivery else 'product',
                'product': product,
            })

    if articles:
        diff = round(round(transaction.amount, 2) - line_total, 2)
        if diff != 0.0:
            articles.append({
                'identifier': 'rounding',
                'description': 'Rounding correction',
                'quantity': 1.0,
                'unit_price_incl': diff,
                'unit_price_excl': diff,
                'vat_percentage': 0.0,
                'vat_amount': 0.0,
                'type': 'rounding',
                'product': None,
            })

    return articles
